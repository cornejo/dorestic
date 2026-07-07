"""Tests for backup execution, lifecycle hooks, and host group backups.

Docker-dependent tests use backup-test.* labels exclusively.
Restic tests use the official restic container image.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import docker
from docker.models.containers import Container

from dorestic import (
    EXIT_ON_START_FAILED,
    BackupConfig,
    ContainerTarget,
    HostGroup,
    ScopeConfig,
    backup_container,
    backup_host_group,
    run_docker_exec,
    run_scope_backup,
)
from tests.conftest import TEST_LABEL_PREFIX, requires_docker, restic_run

DUMMY_CONFIG = BackupConfig(repository="/dummy", password_file="/dummy")


# ── run_scope_backup ────────────────────────────────────────


class TestRunScopeBackupUnit:
    def test_empty_paths_returns_zero(self) -> None:
        code = run_scope_backup("test:container", [], [], config=DUMMY_CONFIG)
        assert code == 0


@requires_docker
class TestRunScopeBackup:
    def test_backs_up_files(
        self,
        backup_config: BackupConfig,
        restic_password_file: Path,
        docker_visible_tmp: Path,
    ) -> None:
        repo_path = Path(backup_config.repository)
        data_dir = docker_visible_tmp / "data"
        data_dir.mkdir()
        (data_dir / "file.txt").write_text("content")

        code = run_scope_backup(
            "test:container", [data_dir], [], config=backup_config,
        )

        assert code == 0

        result = restic_run(
            "snapshots", "--tag", "test:container", "--json",
            repo=repo_path, password_file=restic_password_file,
        )
        assert "test:container" in result.stdout

    def test_exclude_applied(
        self,
        backup_config: BackupConfig,
        restic_password_file: Path,
        docker_visible_tmp: Path,
    ) -> None:
        repo_path = Path(backup_config.repository)
        data_dir = docker_visible_tmp / "data_excl"
        data_dir.mkdir()
        (data_dir / "keep.txt").write_text("keep")
        (data_dir / "skip.log").write_text("skip")

        code = run_scope_backup(
            "test:excl", [data_dir], ["*.log"], config=backup_config,
        )

        assert code == 0

        result = restic_run(
            "ls", "latest", "--tag", "test:excl",
            repo=repo_path, password_file=restic_password_file,
            extra_volumes={str(data_dir): str(data_dir)},
        )
        assert "keep.txt" in result.stdout
        assert "skip.log" not in result.stdout

    def test_multiple_paths(
        self,
        backup_config: BackupConfig,
        docker_visible_tmp: Path,
    ) -> None:
        dir_a = docker_visible_tmp / "a"
        dir_b = docker_visible_tmp / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "from_a.txt").write_text("a")
        (dir_b / "from_b.txt").write_text("b")

        code = run_scope_backup(
            "test:multi", [dir_a, dir_b], [], config=backup_config,
        )

        assert code == 0


# ── backup_container lifecycle ──────────────────────────────


@requires_docker
class TestBackupContainerLifecycle:
    def test_on_start_creates_file(
        self, test_container_with_hooks: tuple[Container, Path],
    ) -> None:
        container, data_dir = test_container_with_hooks
        name = container.name or "unknown"

        with patch("dorestic.backup.run_scope_backup", return_value=0):
            backup_container(
                ContainerTarget(
                    name=name,
                    container=container,
                    container_scope=ScopeConfig(
                        paths=["/data"],
                        on_start="echo 'starting' > /data/hook_started",
                        on_complete="echo $2 > /data/hook_completed",
                    ),
                ),
                config=DUMMY_CONFIG,
            )

        assert (data_dir / "hook_started").exists()
        assert "starting" in (data_dir / "hook_started").read_text()

    def test_on_complete_receives_exit_code(
        self, test_container_with_hooks: tuple[Container, Path],
    ) -> None:
        container, data_dir = test_container_with_hooks
        name = container.name or "unknown"

        with patch("dorestic.backup.run_scope_backup", return_value=0):
            backup_container(
                ContainerTarget(
                    name=name,
                    container=container,
                    container_scope=ScopeConfig(
                        paths=["/data"],
                        on_start="echo 'starting' > /data/hook_started",
                        on_complete="echo $2 > /data/hook_completed",
                    ),
                ),
                config=DUMMY_CONFIG,
            )

        assert (data_dir / "hook_completed").exists()
        completed_text = (data_dir / "hook_completed").read_text().strip()
        assert completed_text == "0"

    def test_failing_on_start_skips_backup(
        self, test_container_failing_hook: tuple[Container, Path],
    ) -> None:
        container, _data_dir = test_container_failing_hook
        name = container.name or "unknown"

        with patch("dorestic.backup.run_scope_backup", return_value=0) as mock_backup:
            container_result, _host_result = backup_container(
                ContainerTarget(
                    name=name,
                    container=container,
                    container_scope=ScopeConfig(
                        paths=["/data"],
                        on_start="exit 1",
                        on_complete="echo $2 > /data/complete_code",
                    ),
                ),
                config=DUMMY_CONFIG,
            )

        mock_backup.assert_not_called()
        assert container_result.exit_code == EXIT_ON_START_FAILED
        assert container_result.skipped is True

    def test_failing_on_start_still_calls_on_complete(
        self, test_container_failing_hook: tuple[Container, Path],
    ) -> None:
        container, hook_data_dir = test_container_failing_hook
        name = container.name or "unknown"

        with patch("dorestic.backup.run_scope_backup", return_value=0):
            backup_container(
                ContainerTarget(
                    name=name,
                    container=container,
                    container_scope=ScopeConfig(
                        paths=["/data"],
                        on_start="exit 1",
                        on_complete="echo $2 > /data/complete_code",
                    ),
                ),
                config=DUMMY_CONFIG,
            )

        assert (hook_data_dir / "complete_code").exists()
        code = (hook_data_dir / "complete_code").read_text().strip()
        assert code == str(EXIT_ON_START_FAILED)

    def test_no_scopes_returns_zero(
        self, test_container: tuple[Container, Path],
    ) -> None:
        container, _ = test_container
        name = container.name or "unknown"

        container_result, host_result = backup_container(
            ContainerTarget(
                name=name,
                container=container,
            ),
            config=DUMMY_CONFIG,
        )
        assert container_result.exit_code == 0
        assert container_result.skipped is True
        assert host_result.exit_code == 0
        assert host_result.skipped is True

    def test_both_scopes_backed_up(
        self, test_container_multi_scope: tuple[Container, Path, Path],
    ) -> None:
        container, _data_dir, compose_dir = test_container_multi_scope
        container.reload()
        name = container.name or "unknown"

        backup_calls: list[tuple[str, list[str], list[str]]] = []

        def mock_backup(tag: str, paths: list[Path], exclude: list[str], **_: Any) -> int:
            backup_calls.append((tag, [str(p) for p in paths], exclude))
            return 0

        with patch("dorestic.backup.run_scope_backup", side_effect=mock_backup):
            container_result, host_result = backup_container(
                ContainerTarget(
                    name=name,
                    container=container,
                    container_scope=ScopeConfig(
                        paths=["/data"],
                        exclude=["*.tmp", "*.log"],
                    ),
                    host_scope=ScopeConfig(
                        paths=[".@1"],
                        exclude=["*.pyc"],
                    ),
                    compose_dir=str(compose_dir),
                ),
                config=DUMMY_CONFIG,
            )

        assert container_result.exit_code == 0
        assert host_result.exit_code == 0
        assert len(backup_calls) == 2

        container_call = next(c for c in backup_calls if ":container" in c[0])
        host_call = next(c for c in backup_calls if ":host" in c[0])

        assert container_call[2] == ["*.tmp", "*.log"]
        assert host_call[2] == ["*.pyc"]

    def test_host_on_start_failure_independent(
        self,
        docker_client: docker.DockerClient,
        docker_visible_tmp: Path,
    ) -> None:
        """Failing host.on_start doesn't affect container scope."""
        data_dir = docker_visible_tmp / "data"
        data_dir.mkdir()
        (data_dir / "file.txt").write_text("x")
        (docker_visible_tmp / "docker-compose.yml").write_text("version: '3'")

        container: Container = docker_client.containers.run(
            "alpine:latest",
            command="sleep 3600",
            labels={f"{TEST_LABEL_PREFIX}.enable": "true"},
            volumes={str(data_dir): {"bind": "/data", "mode": "rw"}},
            detach=True,
        )
        try:
            backup_calls: list[str] = []

            def mock_backup(tag: str, paths: list[Path], exclude: list[str], **_: Any) -> int:
                backup_calls.append(tag)
                return 0

            name = container.name or "unknown"
            with patch("dorestic.backup.run_scope_backup", side_effect=mock_backup):
                _container_result, host_result = backup_container(
                    ContainerTarget(
                        name=name,
                        container=container,
                        container_scope=ScopeConfig(paths=["/data"]),
                        host_scope=ScopeConfig(
                            paths=[".@1"],
                            on_start="exit 1",
                        ),
                        compose_dir=str(docker_visible_tmp),
                    ),
                    config=DUMMY_CONFIG,
                )

            assert any(":container" in c for c in backup_calls)
            assert not any(":host" in c for c in backup_calls)
            assert host_result.exit_code == EXIT_ON_START_FAILED
        finally:
            container.stop(timeout=1)
            container.remove(force=True)


# ── backup_host_group ───────────────────────────────────────


class TestBackupHostGroup:
    def test_skips_when_no_valid_paths(self) -> None:
        group = HostGroup(
            tag="missing",
            paths=["/nonexistent/path"],
        )
        result = backup_host_group(group, config=DUMMY_CONFIG)
        assert result.exit_code == 0
        assert result.skipped is True

    def test_backs_up_valid_paths(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "file.txt").write_text("content")

        backup_calls: list[tuple[str, list[Path], list[str]]] = []

        def mock_backup(tag: str, paths: list[Path], exclude: list[str], **_: Any) -> int:
            backup_calls.append((tag, paths, exclude))
            return 0

        with patch("dorestic.backup.run_scope_backup", side_effect=mock_backup):
            group = HostGroup(tag="docs", paths=[str(data_dir)])
            result = backup_host_group(group, config=DUMMY_CONFIG)

        assert result.exit_code == 0
        assert len(backup_calls) == 1
        assert backup_calls[0][0] == "docs"

    def test_exclude_passed_through(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "file.txt").write_text("x")

        backup_calls: list[tuple[str, list[Path], list[str]]] = []

        def mock_backup(tag: str, paths: list[Path], exclude: list[str], **_: Any) -> int:
            backup_calls.append((tag, paths, exclude))
            return 0

        with patch("dorestic.backup.run_scope_backup", side_effect=mock_backup):
            group = HostGroup(
                tag="t", paths=[str(data_dir)], exclude=["*.log", "cache/"]
            )
            backup_host_group(group, config=DUMMY_CONFIG)

        assert backup_calls[0][2] == ["*.log", "cache/"]

    def test_on_start_failure_skips_backup(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "file.txt").write_text("x")

        script = tmp_path / "fail.sh"
        script.write_text("#!/bin/sh\nexit 1\n")
        script.chmod(0o755)

        with patch("dorestic.backup.run_scope_backup") as mock_backup:
            group = HostGroup(
                tag="t",
                paths=[str(data_dir)],
                on_start=str(script),
            )
            result = backup_host_group(group, config=DUMMY_CONFIG)

        mock_backup.assert_not_called()
        assert result.exit_code == EXIT_ON_START_FAILED
        assert result.skipped is True

    def test_on_start_failure_still_calls_on_complete(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "file.txt").write_text("x")

        fail_script = tmp_path / "fail.sh"
        fail_script.write_text("#!/bin/sh\nexit 1\n")
        fail_script.chmod(0o755)

        complete_marker = tmp_path / "completed"
        complete_script = tmp_path / "complete.sh"
        complete_script.write_text(
            f"#!/bin/sh\necho $2 > {complete_marker}\n"
        )
        complete_script.chmod(0o755)

        with patch("dorestic.backup.run_scope_backup"):
            group = HostGroup(
                tag="t",
                paths=[str(data_dir)],
                on_start=str(fail_script),
                on_complete=str(complete_script),
            )
            backup_host_group(group, config=DUMMY_CONFIG)

        assert complete_marker.exists()
        assert complete_marker.read_text().strip() == str(EXIT_ON_START_FAILED)

    def test_on_complete_receives_exit_code(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "file.txt").write_text("x")

        marker = tmp_path / "complete_code"
        complete_script = tmp_path / "complete.sh"
        complete_script.write_text(f"#!/bin/sh\necho $2 > {marker}\n")
        complete_script.chmod(0o755)

        with patch("dorestic.backup.run_scope_backup", return_value=0):
            group = HostGroup(
                tag="t",
                paths=[str(data_dir)],
                on_complete=str(complete_script),
            )
            backup_host_group(group, config=DUMMY_CONFIG)

        assert marker.exists()
        assert marker.read_text().strip() == "0"

    def test_on_start_success_allows_backup(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "file.txt").write_text("x")

        ok_script = tmp_path / "ok.sh"
        ok_script.write_text("#!/bin/sh\nexit 0\n")
        ok_script.chmod(0o755)

        with patch("dorestic.backup.run_scope_backup", return_value=0) as mock_backup:
            group = HostGroup(
                tag="t",
                paths=[str(data_dir)],
                on_start=str(ok_script),
            )
            result = backup_host_group(group, config=DUMMY_CONFIG)

        mock_backup.assert_called_once()
        assert result.exit_code == 0

    def test_on_start_receives_tag(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "file.txt").write_text("x")

        marker = tmp_path / "tag_received"
        script = tmp_path / "capture_tag.sh"
        script.write_text(f"#!/bin/sh\necho $2 > {marker}\n")
        script.chmod(0o755)

        with patch("dorestic.backup.run_scope_backup", return_value=0):
            group = HostGroup(
                tag="my-tag",
                paths=[str(data_dir)],
                on_start=str(script),
            )
            backup_host_group(group, config=DUMMY_CONFIG)

        assert marker.exists()
        assert marker.read_text().strip() == "my-tag"

    def test_on_complete_receives_tag(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "file.txt").write_text("x")

        marker = tmp_path / "tag_received"
        script = tmp_path / "capture.sh"
        script.write_text(f"#!/bin/sh\necho $4 > {marker}\n")
        script.chmod(0o755)

        with patch("dorestic.backup.run_scope_backup", return_value=0):
            group = HostGroup(
                tag="my-tag",
                paths=[str(data_dir)],
                on_complete=str(script),
            )
            backup_host_group(group, config=DUMMY_CONFIG)

        assert marker.exists()
        assert marker.read_text().strip() == "my-tag"


# ── End-to-end with restic ──────────────────────────────────


@requires_docker
class TestEndToEndRestic:
    def test_full_backup_and_snapshot(
        self,
        backup_config: BackupConfig,
        restic_password_file: Path,
        docker_visible_tmp: Path,
    ) -> None:
        """Verify a backup creates a real restic snapshot with the correct tag."""
        repo_path = Path(backup_config.repository)

        data_dir = docker_visible_tmp / "mydata"
        data_dir.mkdir()
        (data_dir / "important.txt").write_text("critical data")
        (data_dir / "ignore.log").write_text("log noise")

        code = run_scope_backup(
            "myapp:container", [data_dir], ["*.log"], config=backup_config,
        )

        assert code == 0

        result = restic_run(
            "snapshots", "--json",
            repo=repo_path, password_file=restic_password_file,
        )
        assert "myapp:container" in result.stdout

        ls_result = restic_run(
            "ls", "latest", "--tag", "myapp:container",
            repo=repo_path, password_file=restic_password_file,
            extra_volumes={str(data_dir): str(data_dir)},
        )
        assert "important.txt" in ls_result.stdout
        assert "ignore.log" not in ls_result.stdout

    def test_two_scopes_create_separate_snapshots(
        self,
        backup_config: BackupConfig,
        restic_password_file: Path,
        docker_visible_tmp: Path,
    ) -> None:
        """Two backup invocations with different tags create separate snapshots."""
        repo_path = Path(backup_config.repository)

        container_data = docker_visible_tmp / "container"
        container_data.mkdir()
        (container_data / "db.sql").write_text("dump")

        host_data = docker_visible_tmp / "host"
        host_data.mkdir()
        (host_data / "compose.yml").write_text("version: 3")

        assert run_scope_backup(
            "myapp:container", [container_data], [], config=backup_config,
        ) == 0
        assert run_scope_backup(
            "myapp:host", [host_data], [], config=backup_config,
        ) == 0

        result = restic_run(
            "snapshots", "--json",
            repo=repo_path, password_file=restic_password_file,
        )
        assert "myapp:container" in result.stdout
        assert "myapp:host" in result.stdout

    def test_exclude_isolation_between_scopes(
        self,
        backup_config: BackupConfig,
        restic_password_file: Path,
        docker_visible_tmp: Path,
    ) -> None:
        """Excludes on one scope don't affect the other."""
        repo_path = Path(backup_config.repository)

        dir_a = docker_visible_tmp / "a"
        dir_b = docker_visible_tmp / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "data.log").write_text("should be excluded in scope a")
        (dir_b / "data.log").write_text("should appear in scope b")

        run_scope_backup("scope_a", [dir_a], ["*.log"], config=backup_config)
        run_scope_backup("scope_b", [dir_b], [], config=backup_config)

        ls_a = restic_run(
            "ls", "latest", "--tag", "scope_a",
            repo=repo_path, password_file=restic_password_file,
            extra_volumes={str(dir_a): str(dir_a)},
        )
        ls_b = restic_run(
            "ls", "latest", "--tag", "scope_b",
            repo=repo_path, password_file=restic_password_file,
            extra_volumes={str(dir_b): str(dir_b)},
        )
        assert "data.log" not in ls_a.stdout
        assert "data.log" in ls_b.stdout


# ── docker_cp fallback ──────────────────────────────────────


@requires_docker
class TestDockerCpFallback:
    def test_unmounted_path_uses_docker_cp(
        self,
        docker_client: docker.DockerClient,
        docker_visible_tmp: Path,
        backup_config: BackupConfig,
    ) -> None:
        """When a container path has no matching mount, docker cp extracts it."""
        container: Container = docker_client.containers.run(
            "alpine:latest",
            command="sh -c 'mkdir -p /app/data && echo secret > /app/data/file.txt && sleep 3600'",
            labels={f"{TEST_LABEL_PREFIX}.enable": "true"},
            detach=True,
        )
        try:
            import time
            for _ in range(20):
                rc, _ = run_docker_exec(container, "test -f /app/data/file.txt")
                if rc == 0:
                    break
                time.sleep(0.5)

            staging_dir = docker_visible_tmp / "staging"
            staging_dir.mkdir()

            backup_calls: list[tuple[str, list[str]]] = []

            def mock_backup(tag: str, paths: list[Path], exclude: list[str], **_: Any) -> int:
                backup_calls.append((tag, [str(p) for p in paths]))
                return 0

            name = container.name or "unknown"
            with patch("dorestic.backup.run_scope_backup", side_effect=mock_backup):
                backup_container(
                    ContainerTarget(
                        name=name,
                        container=container,
                        container_scope=ScopeConfig(paths=["/app/data"]),
                    ),
                    config=backup_config,
                    staging_dir=staging_dir,
                )

            assert len(backup_calls) == 1
            staged_path = backup_calls[0][1][0]
            assert "staging" in staged_path
            assert Path(staged_path).exists()
            assert (Path(staged_path) / "file.txt").read_text().strip() == "secret"
        finally:
            container.stop(timeout=1)
            container.remove(force=True)
