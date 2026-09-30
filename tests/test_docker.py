"""Tests that require a running Docker daemon.

All containers use the backup-test.* label prefix to ensure complete
isolation from any production backup.enable containers.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import docker
import pytest
from docker.models.containers import Container

from dorestic import (
    ContainerTarget,
    ScopeConfig,
    discover_targets,
    resolve_container_path,
    resolve_container_paths,
    resolve_host_paths,
    run_docker_exec,
    stable_mount_target,
)
from tests.conftest import (
    TEST_LABEL_PREFIX,
    requires_docker,
    start_test_container,
    stop_test_container,
)


# ── discover_targets ────────────────────────────────────────


@requires_docker
class TestDiscoverTargets:
    def test_finds_labeled_container(self, docker_client: docker.DockerClient, test_container: tuple[Container, Path]) -> None:
        container, _ = test_container
        targets = discover_targets(docker_client, label_prefix=TEST_LABEL_PREFIX)
        names = [t.name for t in targets]
        assert container.name in names

    def test_ignores_different_prefix(self, docker_client: docker.DockerClient, test_container: tuple[Container, Path]) -> None:
        """backup-test.* containers are NOT found with a different prefix."""
        targets = discover_targets(docker_client, label_prefix="dorestic-no-match-4f9a2b")
        test_names = {t.name for t in targets}
        container, _ = test_container
        assert container.name not in test_names

    def test_parses_container_scope(self, docker_client: docker.DockerClient, test_container: tuple[Container, Path]) -> None:
        container, _ = test_container
        targets = discover_targets(docker_client, label_prefix=TEST_LABEL_PREFIX)
        target = next(t for t in targets if t.name == container.name)
        assert target.container_scope is not None
        assert "/data" in target.container_scope.paths

    def test_parses_exclude(self, docker_client: docker.DockerClient, test_container_multi_scope: tuple[Container, Path, Path]) -> None:
        container, _, _ = test_container_multi_scope
        targets = discover_targets(docker_client, label_prefix=TEST_LABEL_PREFIX)
        target = next(t for t in targets if t.name == container.name)

        assert target.container_scope is not None
        assert "*.tmp" in target.container_scope.exclude
        assert "*.log" in target.container_scope.exclude

        assert target.host_scope is not None
        assert "*.pyc" in target.host_scope.exclude

    def test_parses_host_scope(self, docker_client: docker.DockerClient, test_container_multi_scope: tuple[Container, Path, Path]) -> None:
        container, _, compose_dir = test_container_multi_scope
        targets = discover_targets(docker_client, label_prefix=TEST_LABEL_PREFIX)
        target = next(t for t in targets if t.name == container.name)

        assert target.host_scope is not None
        assert ".@1" in target.host_scope.paths
        assert target.compose_dir == str(compose_dir)

    def test_parses_hooks(self, docker_client: docker.DockerClient, test_container_with_hooks: tuple[Container, Path]) -> None:
        container, _ = test_container_with_hooks
        targets = discover_targets(docker_client, label_prefix=TEST_LABEL_PREFIX)
        target = next(t for t in targets if t.name == container.name)

        assert target.container_scope is not None
        assert target.container_scope.on_start is not None
        assert "hook_started" in target.container_scope.on_start
        assert target.container_scope.on_complete is not None

    def test_no_host_scope_when_not_labeled(self, docker_client: docker.DockerClient, test_container: tuple[Container, Path]) -> None:
        container, _ = test_container
        targets = discover_targets(docker_client, label_prefix=TEST_LABEL_PREFIX)
        target = next(t for t in targets if t.name == container.name)
        assert target.host_scope is None

    def test_parses_container_shell(self, docker_client: docker.DockerClient, docker_visible_tmp: Path) -> None:
        """backup.container.shell label is parsed into ScopeConfig.shell."""
        data_dir = docker_visible_tmp / "shell_data"
        data_dir.mkdir()
        container = start_test_container(
            docker_client,
            labels={
                f"{TEST_LABEL_PREFIX}.enable": "true",
                f"{TEST_LABEL_PREFIX}.container.paths": "/data",
                f"{TEST_LABEL_PREFIX}.container.shell": "/bin/bash",
            },
            binds={data_dir: "/data"},
        )
        try:
            targets = discover_targets(docker_client, label_prefix=TEST_LABEL_PREFIX)
            target = next(t for t in targets if t.name == container.name)
            assert target.container_scope is not None
            assert target.container_scope.shell == "/bin/bash"
        finally:
            stop_test_container(container)

    def test_no_paths_raises(self, docker_client: docker.DockerClient, docker_visible_tmp: Path) -> None:
        """backup.enable=true without any paths raises ValueError."""
        container = start_test_container(
            docker_client,
            labels={f"{TEST_LABEL_PREFIX}.enable": "true"},
        )
        try:
            with pytest.raises(ValueError, match="no container.paths or host.paths"):
                discover_targets(docker_client, label_prefix=TEST_LABEL_PREFIX)
        finally:
            stop_test_container(container)

    def test_excludes_label_typo_raises(self, docker_client: docker.DockerClient, docker_visible_tmp: Path) -> None:
        """Using 'excludes' (plural) in a Docker label raises a clear error."""
        container = start_test_container(
            docker_client,
            labels={
                f"{TEST_LABEL_PREFIX}.enable": "true",
                f"{TEST_LABEL_PREFIX}.container.paths": "/data",
                f"{TEST_LABEL_PREFIX}.container.excludes": "*.tmp",
            },
        )
        try:
            with pytest.raises(ValueError, match="excludes.*plural"):
                discover_targets(docker_client, label_prefix=TEST_LABEL_PREFIX)
        finally:
            stop_test_container(container)


# ── resolve_container_path ──────────────────────────────────


@requires_docker
class TestResolveContainerPath:
    def test_resolves_mounted_path(self, test_container: tuple[Container, Path]) -> None:
        container, data_dir = test_container
        container.reload()

        result = resolve_container_path(container, "/data", suppress_warning=False)
        assert result is not None
        assert result == data_dir

    def test_resolves_subpath(self, test_container: tuple[Container, Path]) -> None:
        container, data_dir = test_container
        container.reload()

        result = resolve_container_path(
            container, "/data/subdir/file.txt", suppress_warning=False
        )
        assert result is not None
        assert str(result).endswith("subdir/file.txt")
        assert str(data_dir) in str(result)

    def test_returns_none_for_unmounted(self, test_container_no_mount: Container) -> None:
        container = test_container_no_mount
        container.reload()

        result = resolve_container_path(container, "/data", suppress_warning=True)
        assert result is None

    def test_suppresses_warning(self, test_container_no_mount: Container, caplog: pytest.LogCaptureFixture) -> None:
        container = test_container_no_mount
        container.reload()

        resolve_container_path(container, "/data", suppress_warning=True)
        assert "no matching mount" not in caplog.text

    def test_warns_when_not_suppressed(self, test_container_no_mount: Container, caplog: pytest.LogCaptureFixture) -> None:
        container = test_container_no_mount
        container.reload()

        with caplog.at_level(logging.WARNING):
            resolve_container_path(container, "/data", suppress_warning=False)
        assert "no matching mount" in caplog.text

    def test_longest_prefix_match(self, docker_client: docker.DockerClient, docker_visible_tmp: Path) -> None:
        """When multiple mounts match, the longest prefix wins."""
        outer_dir = docker_visible_tmp / "outer"
        inner_dir = docker_visible_tmp / "inner"
        outer_dir.mkdir()
        inner_dir.mkdir()

        container = start_test_container(
            docker_client,
            labels={f"{TEST_LABEL_PREFIX}.enable": "true"},
            binds={outer_dir: "/data", inner_dir: "/data/nested"},
        )
        try:
            container.reload()
            result = resolve_container_path(
                container, "/data/nested/file.txt", suppress_warning=False
            )
            assert result is not None
            assert str(inner_dir) in str(result)
            assert str(result).endswith("file.txt")
        finally:
            stop_test_container(container)


# ── resolve_container_paths (with existence check) ──────────


@requires_docker
class TestResolveContainerPaths:
    def test_resolves_existing_paths(self, test_container: tuple[Container, Path]) -> None:
        container, data_dir = test_container
        container.reload()

        target = ContainerTarget(
            name=container.name or "unknown",
            container=container,
            container_scope=ScopeConfig(paths=["/data"]),
        )
        resolved = resolve_container_paths(target)
        assert len(resolved) == 1
        assert resolved[0].source == data_dir
        assert resolved[0].daemon_sourced

    def test_source_is_pinned_to_a_stable_target(
        self, test_container: tuple[Container, Path],
    ) -> None:
        """The snapshot path must not follow the source.

        A daemon may report an instance-scoped source — a fresh path per mount,
        so a recreated container reports a different one for the same
        directory. Recording that would start a new snapshot lineage every
        recreation and force a full rescan each time.
        """
        container, _ = test_container
        container.reload()

        target = ContainerTarget(
            name=container.name or "unknown",
            container=container,
            container_scope=ScopeConfig(paths=["/data"]),
        )
        resolved = resolve_container_paths(target)

        assert resolved[0].target == stable_mount_target("/data")
        assert resolved[0].remapped

    def test_source_is_kept_even_when_unreadable(
        self, test_container: tuple[Container, Path], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mounts[].Source is a daemon-namespace path and is never stat'd here.

        This is the regression: `Path.exists()` returns False on EACCES just as
        it does for a missing path, so an unreadable-but-valid source was
        dropped with a misleading "does not exist" and the target failed with
        "none of the configured paths resolved" — data loss reported as a
        missing path. The daemon can read it; we never need to.
        """
        container, _ = test_container
        container.reload()

        def refuse(*_args: object, **_kwargs: object) -> bool:
            raise AssertionError("daemon-sourced path must not be stat'd locally")

        monkeypatch.setattr(Path, "exists", refuse)

        target = ContainerTarget(
            name=container.name or "unknown",
            container=container,
            container_scope=ScopeConfig(paths=["/data"]),
        )
        assert len(resolve_container_paths(target)) == 1

    def test_skips_unmounted_paths(self, test_container: tuple[Container, Path]) -> None:
        container, _ = test_container
        container.reload()

        target = ContainerTarget(
            name=container.name or "unknown",
            container=container,
            container_scope=ScopeConfig(paths=["/not-mounted"]),
            suppress_mount_warning=True,
        )
        resolved = resolve_container_paths(target)
        assert len(resolved) == 0


# ── resolve_host_paths ──────────────────────────────────────


@requires_docker
class TestResolveHostPaths:
    def test_resolves_depth_limited(self, test_container_multi_scope: tuple[Container, Path, Path]) -> None:
        container, _, compose_dir = test_container_multi_scope

        target = ContainerTarget(
            name=container.name or "unknown",
            container=container,
            host_scope=ScopeConfig(paths=[".@1"]),
            compose_dir=str(compose_dir),
        )
        resolved = resolve_host_paths(target)
        names = {p.name for p in resolved}
        assert "docker-compose.yml" in names
        assert ".env" in names

    def test_returns_empty_without_compose_dir(self, test_container: tuple[Container, Path]) -> None:
        container, _ = test_container
        target = ContainerTarget(
            name=container.name or "unknown",
            container=container,
            host_scope=ScopeConfig(paths=[".@1"]),
            compose_dir=None,
        )
        resolved = resolve_host_paths(target)
        assert resolved == []


# ── run_docker_exec ─────────────────────────────────────────


@requires_docker
class TestRunDockerExec:
    def test_successful_command(self, test_container: tuple[Container, Path]) -> None:
        container, _ = test_container
        code, output = run_docker_exec(container, "echo hello")
        assert code == 0
        assert "hello" in output

    def test_failed_command(self, test_container: tuple[Container, Path]) -> None:
        container, _ = test_container
        code, _output = run_docker_exec(container, "exit 42")
        assert code == 42

    def test_command_with_output(self, test_container: tuple[Container, Path]) -> None:
        container, _ = test_container
        code, output = run_docker_exec(container, "ls /data")
        assert code == 0
        assert "important.db" in output

    def test_writes_file(self, test_container: tuple[Container, Path]) -> None:
        container, data_dir = test_container
        code, _ = run_docker_exec(container, "echo test_data > /data/created.txt")
        assert code == 0
        assert (data_dir / "created.txt").exists()
        assert "test_data" in (data_dir / "created.txt").read_text()

    def test_stderr_captured(self, test_container: tuple[Container, Path]) -> None:
        container, _ = test_container
        _, output = run_docker_exec(container, "echo err >&2")
        assert "err" in output

    def test_multiline_output(self, test_container: tuple[Container, Path]) -> None:
        container, _ = test_container
        code, output = run_docker_exec(container, "echo line1; echo line2")
        assert code == 0
        lines = output.splitlines()
        assert len(lines) == 2

    def test_env_vars_passed(self, test_container: tuple[Container, Path]) -> None:
        container, _ = test_container
        code, output = run_docker_exec(
            container, "echo $DORESTIC_TAG",
            env={"DORESTIC_TAG": "myapp"},
        )
        assert code == 0
        assert "myapp" in output

    def test_custom_shell(self, test_container: tuple[Container, Path]) -> None:
        container, _ = test_container
        code, output = run_docker_exec(container, "echo hello", shell="ash")
        assert code == 0
        assert "hello" in output


# ── docker --mount CSV behaviour ────────────────────────────


@requires_docker
class TestMountCsvBehaviour:
    """Pins the assumption behind EXIT_UNMOUNTABLE_PATH.

    `unmountable_paths()` rejects a comma because `docker --mount` parses its
    value as CSV. That is a property of the docker CLI, not of dorestic, so it
    can change under us — and the rejection is invisible until someone has a
    path with a comma in it. If a future docker learns to quote, this test
    fails and the restriction can be lifted rather than quietly outliving its
    reason.
    """

    def test_mount_cannot_express_a_comma(self, tmp_path: Path) -> None:
        odd = tmp_path / "with,comma"
        odd.mkdir()
        (odd / "f.txt").write_text("x")

        for src in (str(odd), f'"{odd}"'):
            result = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "--mount", f"type=bind,src={src},dst=/probe,readonly",
                    "alpine", "ls", "/probe",
                ],
                capture_output=True, text=True,
            )
            assert result.returncode != 0, f"docker now accepts {src!r} — revisit"
            assert "invalid" in (result.stderr + result.stdout).lower()

    def test_v_still_accepts_a_comma(self, tmp_path: Path) -> None:
        """The other half: `-v` handles it, which is why it looked tempting."""
        odd = tmp_path / "with,comma"
        odd.mkdir()
        (odd / "f.txt").write_text("x")

        result = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{odd}:/probe:ro", "alpine", "ls", "/probe"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"daemon refuses the bind mount: {result.stderr.strip()}")
        assert "f.txt" in result.stdout
