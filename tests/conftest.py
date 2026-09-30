from __future__ import annotations

import functools
import shutil
import subprocess
import uuid
import warnings
from collections.abc import Generator
from pathlib import Path
from typing import Any, TypeVar, cast

import docker
import docker.errors
import pytest
from docker.models.containers import Container

from dorestic import BackupConfig

TEST_LABEL_PREFIX = "backup-test"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESTIC_IMAGE = "restic/restic:latest"


# ── container creation ─────────────────────────────────────


def start_test_container(
    client: docker.DockerClient,
    *,
    labels: dict[str, str],
    binds: dict[Path, str] | None = None,
    image: str = "alpine:latest",
    command: str = "sleep 3600",
) -> Container:
    """Start a detached test container, bind-mounting host paths by path.

    Deliberately the low-level API rather than `client.containers.run(volumes=)`.
    The high-level call sends the legacy top-level `Volumes` map alongside
    `HostConfig.Binds` — that map declares *anonymous volumes*, which these tests
    never want, and a socket proxy enforcing a no-volumes policy rejects the
    whole request on account of it. Binds alone say exactly what is meant: mount
    this host path at this container path.

    Every bind source must live under the project root, so a daemon that shares
    this filesystem resolves it and the whole tree can be cleaned up afterwards.
    """
    for host in binds or {}:
        assert PROJECT_ROOT in host.parents, (
            f"bind source {host} is outside the project root"
        )

    # docker-py's low-level API carries no type information.
    api: Any = client.api
    host_config: dict[str, Any] = api.create_host_config(
        binds={
            str(host): {"bind": container_path, "mode": "rw"}
            for host, container_path in (binds or {}).items()
        }
    )
    created: dict[str, str] = api.create_container(
        image,
        command=command,
        labels=labels,
        host_config=host_config,
        detach=True,
    )
    container: Container = client.containers.get(created["Id"])
    container.start()
    return container


def stop_test_container(container: Container) -> None:
    """Best-effort teardown; a test that already removed it must not fail here."""
    try:
        container.stop(timeout=1)
    except Exception:
        pass
    try:
        container.remove(force=True)
    except Exception:
        pass


# ── Skip markers for external dependencies ──────────────────

@functools.cache
def docker_unusable_reason() -> str | None:
    """Return why Docker can't run these tests, or None if it can.

    A reachable daemon is not enough. Every Docker-backed test here bind-mounts
    a directory out of the repository, and that fails in two ways a plain
    `docker info` cannot see:

      * a socket proxy or daemon policy that refuses volumes outright, and
      * a *sibling* daemon, where the job's own path for the workspace is not
        the path the daemon resolves — the mount silently lands on an empty or
        wrong directory.

    So the probe actually mounts a file and reads it back. Getting this wrong in
    either direction is expensive: too weak and the suite reports dozens of
    confusing errors, too strong and real breakage is hidden behind a skip.

    This covers directory mounts, which is what the Docker-backed tests need.
    The restic-backed ones additionally need a *file* mount — see
    `restic_unusable_reason`.
    """
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, check=False)
    except OSError:
        # No docker binary at all — subprocess raises rather than returning
        # non-zero, which would abort collection.
        return "no docker binary on PATH"
    if info.returncode != 0:
        return "docker daemon not reachable"

    # Under the project root, like every mount the fixtures make, and inside the
    # gitignored tmp/ so a crashed run leaves nothing tracked behind.
    tmp_root = PROJECT_ROOT / "tmp"
    tmp_root.mkdir(exist_ok=True)
    probe_dir = tmp_root / f".probe-{uuid.uuid4().hex[:8]}"
    probe_dir.mkdir()
    sentinel = uuid.uuid4().hex
    container: Container | None = None
    try:
        (probe_dir / "probe").write_text(sentinel)
        # Exercise the same path the fixtures take, so the probe's answer is
        # actually predictive of whether they will work.
        client = docker.DockerClient.from_env()
        try:
            container = start_test_container(
                client,
                labels={},
                binds={probe_dir: "/probe"},
                command="cat /probe/probe",
            )
            container.wait(timeout=30)
            output = container.logs(stdout=True, stderr=False)
        except docker.errors.DockerException as e:
            return f"cannot bind-mount the workspace: {e}"
        finally:
            if container is not None:
                stop_test_container(container)
            client.close()
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
        # Only remove tmp/ if this probe is all that was in it.
        try:
            tmp_root.rmdir()
        except OSError:
            pass

    if output.decode().strip() != sentinel:
        return (
            "bind mount resolved to the wrong directory — the daemon sees a "
            "different filesystem than this process (sibling-container runner "
            "without a matching builds_dir?)"
        )
    return None


def restic_unusable_reason() -> str | None:
    """Return why the restic-backed tests can't run, or None if they can.

    Everything `docker_unusable_reason` covers, plus a single *file* bind
    mount: `_build_restic_cmd` mounts the restic password file directly, and a
    daemon can allow directory mounts while refusing file ones. On such a host
    every restic-backed test skips itself from inside a fixture — invisibly —
    while the directory probe reports everything fine. Kept separate from
    `docker_unusable_reason` so this does not disqualify the Docker-backed
    tests that only need directories and run there perfectly well.
    """
    reason = docker_unusable_reason()
    if reason is not None:
        return reason

    tmp_root = PROJECT_ROOT / "tmp"
    tmp_root.mkdir(exist_ok=True)
    probe_dir = tmp_root / f".fileprobe-{uuid.uuid4().hex[:8]}"
    probe_dir.mkdir()
    probe_file = probe_dir / "probe"
    sentinel = uuid.uuid4().hex
    try:
        probe_file.write_text(sentinel)
        result = subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", f"{probe_file}:/probe:ro",
                "alpine", "cat", "/probe",
            ],
            capture_output=True, text=True, check=False,
        )
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
        try:
            tmp_root.rmdir()
        except OSError:
            pass

    if result.returncode != 0 or result.stdout.strip() != sentinel:
        detail = result.stderr.strip() or result.stdout.strip()
        return f"cannot bind-mount a single file (restic's password file): {detail}"
    return None


def _docker_available() -> bool:
    return docker_unusable_reason() is None


_docker_skip: pytest.MarkDecorator = pytest.mark.skipif(
    not _docker_available(),
    reason=f"Docker unusable: {docker_unusable_reason()}",
)

_T = TypeVar("_T")


def requires_docker(obj: _T) -> _T:
    """Tag a test with the `docker` marker and skip it when no daemon is reachable.

    Applying both means `-m docker` selects these tests even on a host without
    Docker (where they report as skipped rather than silently vanishing).
    """
    return cast("_T", _docker_skip(pytest.mark.docker(obj)))


# Fixtures whose use implies an external dependency. Markers are derived from
# these at collection time so `-m docker` / `-m restic` stay correct as tests are
# added, without needing a decorator on every new class.
_DOCKER_FIXTURES = frozenset({
    "docker_client",
    "shared_tmp_dir",
    "docker_visible_tmp",
    "test_container",
    "test_container_with_hooks",
    "test_container_failing_hook",
    "test_container_no_mount",
    "test_container_multi_scope",
})
_RESTIC_FIXTURES = frozenset({
    "restic_repo",
    "restic_password_file",
    "backup_config",
})


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        fixtures = set(getattr(item, "fixturenames", ()))
        needs_restic = bool(fixtures & _RESTIC_FIXTURES)
        if needs_restic:
            item.add_marker(pytest.mark.restic)
        # restic is invoked through its official Docker image, so anything
        # needing restic needs Docker too.
        if needs_restic or fixtures & _DOCKER_FIXTURES:
            item.add_marker(pytest.mark.docker)



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
    reason = docker_unusable_reason()
    if reason is not None:
        pytest.skip(f"Docker unusable: {reason}")
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

    container = start_test_container(
        docker_client,
        labels={
            f"{TEST_LABEL_PREFIX}.enable": "true",
            f"{TEST_LABEL_PREFIX}.container.paths": "/data",
        },
        binds={data_dir: "/data"},
    )

    yield container, data_dir

    stop_test_container(container)


@pytest.fixture
def test_container_with_hooks(
    docker_client: docker.DockerClient, docker_visible_tmp: Path,
) -> Generator[tuple[Container, Path], None, None]:
    """Create a test container with on_start and on_complete hooks."""
    data_dir = docker_visible_tmp / "hook_data"
    data_dir.mkdir()

    container = start_test_container(
        docker_client,
        labels={
            f"{TEST_LABEL_PREFIX}.enable": "true",
            f"{TEST_LABEL_PREFIX}.container.paths": "/data",
            f"{TEST_LABEL_PREFIX}.container.on_start": "echo 'starting' > /data/hook_started",
            f"{TEST_LABEL_PREFIX}.container.on_complete": "echo $DORESTIC_EXIT_CODE > /data/hook_completed",
        },
        binds={data_dir: "/data"},
    )

    yield container, data_dir

    stop_test_container(container)


@pytest.fixture
def test_container_failing_hook(
    docker_client: docker.DockerClient, docker_visible_tmp: Path,
) -> Generator[tuple[Container, Path], None, None]:
    """Create a test container with an on_start that fails."""
    data_dir = docker_visible_tmp / "fail_data"
    data_dir.mkdir()
    (data_dir / "important.db").write_text("database content")

    container = start_test_container(
        docker_client,
        labels={
            f"{TEST_LABEL_PREFIX}.enable": "true",
            f"{TEST_LABEL_PREFIX}.container.paths": "/data",
            f"{TEST_LABEL_PREFIX}.container.on_start": "exit 1",
            f"{TEST_LABEL_PREFIX}.container.on_complete": "echo $DORESTIC_EXIT_CODE > /data/complete_code",
        },
        binds={data_dir: "/data"},
    )

    yield container, data_dir

    stop_test_container(container)


@pytest.fixture
def test_container_no_mount(
    docker_client: docker.DockerClient,
) -> Generator[Container, None, None]:
    """Create a test container with backup-test.enable but no volume mounts."""
    container = start_test_container(
        docker_client,
        labels={
            f"{TEST_LABEL_PREFIX}.enable": "true",
            f"{TEST_LABEL_PREFIX}.container.paths": "/data",
        },
    )

    yield container

    stop_test_container(container)


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

    container = start_test_container(
        docker_client,
        labels={
            f"{TEST_LABEL_PREFIX}.enable": "true",
            f"{TEST_LABEL_PREFIX}.container.paths": "/data",
            f"{TEST_LABEL_PREFIX}.container.exclude": "*.tmp,*.log",
            f"{TEST_LABEL_PREFIX}.host.paths": ".@1",
            f"{TEST_LABEL_PREFIX}.host.exclude": "*.pyc",
            "com.docker.compose.project.working_dir": str(compose_dir),
        },
        binds={data_dir: "/data"},
    )

    yield container, data_dir, compose_dir

    stop_test_container(container)


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
