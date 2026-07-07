from __future__ import annotations

import shutil
import subprocess
import uuid
import warnings
from collections.abc import Generator
from pathlib import Path

import docker
import pytest
from docker.models.containers import Container

from dorestic import BackupConfig

# ── Skip markers for external dependencies ──────────────────

def _docker_available() -> bool:
    result = subprocess.run(
        ["docker", "info"], capture_output=True, check=False,
    )
    return result.returncode == 0


requires_docker: pytest.MarkDecorator = pytest.mark.skipif(
    not _docker_available(), reason="Docker daemon not available"
)

TEST_LABEL_PREFIX = "backup-test"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESTIC_IMAGE = "restic/restic:latest"


# ── Helpers ────────────────────────────────────────────────


def restic_run(
    *args: str,
    repo: Path,
    password_file: Path,
    extra_volumes: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a restic command via the official Docker container."""
    password_mount = "/run/secrets/restic-password"
    cmd: list[str] = [
        "docker", "run", "--rm",
        "-e", f"RESTIC_REPOSITORY={repo}",
        "-e", f"RESTIC_PASSWORD_FILE={password_mount}",
        "-v", f"{password_file}:{password_mount}:ro",
        "-v", f"{repo}:{repo}",
    ]
    if extra_volumes:
        for host_path, container_path in extra_volumes.items():
            cmd.extend(["-v", f"{host_path}:{container_path}"])
    cmd.extend([RESTIC_IMAGE, *args])
    return subprocess.run(cmd, capture_output=True, text=True)


# ── Fixtures ────────────────────────────────────────────────


def _force_remove_dir(path: Path) -> None:
    """Remove a directory that may contain root-owned files from Docker containers."""
    try:
        shutil.rmtree(path)
    except PermissionError:
        subprocess.run(
            ["docker", "run", "--rm", "-v", f"{path}:/cleanup", "alpine:latest",
             "rm", "-rf", "/cleanup"],
            capture_output=True,
        )
        path.rmdir()


@pytest.fixture(scope="session")
def shared_tmp_dir() -> Generator[Path, None, None]:
    """Session-wide tmp/ directory visible to Docker."""
    tmp_dir = PROJECT_ROOT / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    yield tmp_dir
    remaining = list(tmp_dir.iterdir())
    if remaining:
        warnings.warn(
            f"tmp/ dir not empty at session end: {[p.name for p in remaining]}",
            stacklevel=1,
        )
    _force_remove_dir(tmp_dir)


@pytest.fixture
def docker_visible_tmp(shared_tmp_dir: Path) -> Generator[Path, None, None]:
    """Per-test temp directory inside tmp/, visible to Docker daemon."""
    tmp_dir = shared_tmp_dir / uuid.uuid4().hex[:12]
    tmp_dir.mkdir()
    yield tmp_dir
    _force_remove_dir(tmp_dir)


@pytest.fixture
def tmp_path_with_files(tmp_path: Path) -> Path:
    """Create a temp directory with some test files."""
    (tmp_path / "file1.txt").write_text("hello")
    (tmp_path / "file2.log").write_text("log data")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "nested.txt").write_text("nested")
    (tmp_path / "subdir" / "deep").mkdir()
    (tmp_path / "subdir" / "deep" / "level2.txt").write_text("deep")
    return tmp_path


@pytest.fixture
def docker_client() -> docker.DockerClient:
    """Return a Docker client, skip if unavailable."""
    if not _docker_available():
        pytest.skip("Docker daemon not available")
    return docker.DockerClient.from_env()


@pytest.fixture
def restic_password_file(docker_visible_tmp: Path) -> Path:
    """Create a password file for restic test operations."""
    pw_file = docker_visible_tmp / "restic-password"
    pw_file.write_text("test-password")
    return pw_file


@pytest.fixture
def restic_repo(
    docker_visible_tmp: Path,
    restic_password_file: Path,
) -> Path:
    """Initialize a temporary restic repository using the Docker restic image."""
    repo_path = docker_visible_tmp / "repo"
    repo_path.mkdir()
    result = restic_run("init", repo=repo_path, password_file=restic_password_file)
    if result.returncode != 0:
        pytest.skip(f"Cannot initialize restic repo: {result.stderr}")
    return repo_path


@pytest.fixture
def backup_config(
    restic_repo: Path,
    restic_password_file: Path,
) -> BackupConfig:
    """A BackupConfig pointing at the test restic repo with a password file."""
    return BackupConfig(
        repository=str(restic_repo),
        password_file=str(restic_password_file),
    )


@pytest.fixture
def test_container(
    docker_client: docker.DockerClient, docker_visible_tmp: Path,
) -> Generator[tuple[Container, Path], None, None]:
    """Create a test Docker container with backup-test.* labels and a volume mount."""
    data_dir = docker_visible_tmp / "container_data"
    data_dir.mkdir()
    (data_dir / "important.db").write_text("database content")
    (data_dir / "cache.tmp").write_text("temporary")

    container: Container = docker_client.containers.run(
        "alpine:latest",
        command="sleep 3600",
        labels={
            f"{TEST_LABEL_PREFIX}.enable": "true",
            f"{TEST_LABEL_PREFIX}.container.paths": "/data",
        },
        volumes={str(data_dir): {"bind": "/data", "mode": "rw"}},
        detach=True,
        remove=False,
    )

    yield container, data_dir

    try:
        container.stop(timeout=1)
    except Exception:
        pass
    try:
        container.remove(force=True)
    except Exception:
        pass


@pytest.fixture
def test_container_with_hooks(
    docker_client: docker.DockerClient, docker_visible_tmp: Path,
) -> Generator[tuple[Container, Path], None, None]:
    """Create a test container with on_start and on_complete hooks."""
    data_dir = docker_visible_tmp / "hook_data"
    data_dir.mkdir()

    container: Container = docker_client.containers.run(
        "alpine:latest",
        command="sleep 3600",
        labels={
            f"{TEST_LABEL_PREFIX}.enable": "true",
            f"{TEST_LABEL_PREFIX}.container.paths": "/data",
            f"{TEST_LABEL_PREFIX}.container.on_start": "echo 'starting' > /data/hook_started",
            f"{TEST_LABEL_PREFIX}.container.on_complete": "echo $DORESTIC_EXIT_CODE > /data/hook_completed",
        },
        volumes={str(data_dir): {"bind": "/data", "mode": "rw"}},
        detach=True,
        remove=False,
    )

    yield container, data_dir

    try:
        container.stop(timeout=1)
    except Exception:
        pass
    try:
        container.remove(force=True)
    except Exception:
        pass


@pytest.fixture
def test_container_failing_hook(
    docker_client: docker.DockerClient, docker_visible_tmp: Path,
) -> Generator[tuple[Container, Path], None, None]:
    """Create a test container with an on_start that fails."""
    data_dir = docker_visible_tmp / "fail_data"
    data_dir.mkdir()
    (data_dir / "important.db").write_text("database content")

    container: Container = docker_client.containers.run(
        "alpine:latest",
        command="sleep 3600",
        labels={
            f"{TEST_LABEL_PREFIX}.enable": "true",
            f"{TEST_LABEL_PREFIX}.container.paths": "/data",
            f"{TEST_LABEL_PREFIX}.container.on_start": "exit 1",
            f"{TEST_LABEL_PREFIX}.container.on_complete": "echo $DORESTIC_EXIT_CODE > /data/complete_code",
        },
        volumes={str(data_dir): {"bind": "/data", "mode": "rw"}},
        detach=True,
        remove=False,
    )

    yield container, data_dir

    try:
        container.stop(timeout=1)
    except Exception:
        pass
    try:
        container.remove(force=True)
    except Exception:
        pass


@pytest.fixture
def test_container_no_mount(
    docker_client: docker.DockerClient,
) -> Generator[Container, None, None]:
    """Create a test container with backup-test.enable but no volume mounts."""
    container: Container = docker_client.containers.run(
        "alpine:latest",
        command="sleep 3600",
        labels={
            f"{TEST_LABEL_PREFIX}.enable": "true",
            f"{TEST_LABEL_PREFIX}.container.paths": "/data",
        },
        detach=True,
        remove=False,
    )

    yield container

    try:
        container.stop(timeout=1)
    except Exception:
        pass
    try:
        container.remove(force=True)
    except Exception:
        pass


@pytest.fixture
def test_container_multi_scope(
    docker_client: docker.DockerClient, docker_visible_tmp: Path,
) -> Generator[tuple[Container, Path, Path], None, None]:
    """Create a test container with both container and host scope labels."""
    data_dir = docker_visible_tmp / "multi_data"
    data_dir.mkdir()
    (data_dir / "app.db").write_text("app data")

    compose_dir = docker_visible_tmp / "compose_project"
    compose_dir.mkdir()
    (compose_dir / "docker-compose.yml").write_text("version: '3'")
    (compose_dir / ".env").write_text("KEY=val")

    container: Container = docker_client.containers.run(
        "alpine:latest",
        command="sleep 3600",
        labels={
            f"{TEST_LABEL_PREFIX}.enable": "true",
            f"{TEST_LABEL_PREFIX}.container.paths": "/data",
            f"{TEST_LABEL_PREFIX}.container.exclude": "*.tmp,*.log",
            f"{TEST_LABEL_PREFIX}.host.paths": ".@1",
            f"{TEST_LABEL_PREFIX}.host.exclude": "*.pyc",
            "com.docker.compose.project.working_dir": str(compose_dir),
        },
        volumes={str(data_dir): {"bind": "/data", "mode": "rw"}},
        detach=True,
        remove=False,
    )

    yield container, data_dir, compose_dir

    try:
        container.stop(timeout=1)
    except Exception:
        pass
    try:
        container.remove(force=True)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def cleanup_test_containers(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    """Safety net: remove any lingering test containers after each test."""
    yield
    if "docker_client" not in request.fixturenames and "docker_visible_tmp" not in request.fixturenames:
        return
    if not _docker_available():
        return
    try:
        client = docker.DockerClient.from_env()
        for container in client.containers.list(
            filters={"label": f"{TEST_LABEL_PREFIX}.enable=true"},
            all=True,
        ):
            container.remove(force=True)
    except Exception:
        pass
