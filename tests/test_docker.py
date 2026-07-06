"""Tests that require a running Docker daemon.

All containers use the backup-test.* label prefix to ensure complete
isolation from any production backup.enable containers.
"""

from __future__ import annotations

import logging
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
)
from tests.conftest import TEST_LABEL_PREFIX, requires_docker


# ── discover_targets ────────────────────────────────────────


@requires_docker
class TestDiscoverTargets:
    def test_finds_labeled_container(self, docker_client: docker.DockerClient, test_container: tuple[Container, Path]) -> None:
        container, _ = test_container
        targets = discover_targets(docker_client, label_prefix=TEST_LABEL_PREFIX)
        names = [t.name for t in targets]
        assert container.name in names

    def test_ignores_production_prefix(self, docker_client: docker.DockerClient, test_container: tuple[Container, Path]) -> None:
        """backup-test.* containers are NOT found with default 'backup' prefix."""
        targets = discover_targets(docker_client, label_prefix="backup")
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

    def test_excludes_label_typo_raises(self, docker_client: docker.DockerClient, docker_visible_tmp: Path) -> None:
        """Using 'excludes' (plural) in a Docker label raises a clear error."""
        container: Container = docker_client.containers.run(
            "alpine:latest",
            command="sleep 3600",
            labels={
                f"{TEST_LABEL_PREFIX}.enable": "true",
                f"{TEST_LABEL_PREFIX}.container.paths": "/data",
                f"{TEST_LABEL_PREFIX}.container.excludes": "*.tmp",
            },
            detach=True,
            remove=False,
        )
        try:
            with pytest.raises(ValueError, match="excludes.*plural"):
                discover_targets(docker_client, label_prefix=TEST_LABEL_PREFIX)
        finally:
            container.stop(timeout=1)
            container.remove(force=True)


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

        container: Container = docker_client.containers.run(
            "alpine:latest",
            command="sleep 3600",
            labels={f"{TEST_LABEL_PREFIX}.enable": "true"},
            volumes={
                str(outer_dir): {"bind": "/data", "mode": "rw"},
                str(inner_dir): {"bind": "/data/nested", "mode": "rw"},
            },
            detach=True,
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
            container.stop(timeout=1)
            container.remove(force=True)


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
        assert resolved[0] == data_dir

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
