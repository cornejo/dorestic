"""Tests for backup execution, lifecycle hooks, and host group backups.

Docker-dependent tests use backup-test.* labels exclusively.
Restic tests use the official restic container image.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import docker
from docker.models.containers import Container

import logging

import pytest

from dorestic import (
    EXIT_NO_PATHS_RESOLVED,
    EXIT_ON_START_FAILED,
    BackupConfig,
    BackupPath,
    ContainerTarget,
    HostGroup,
    ScopeConfig,
    ScopeResult,
    backup_container,
    backup_host_group,
    make_restic_hostname,
    run_docker_exec,
    run_scope_backup,
    stable_mount_target,
)
from dorestic.backup import orchestrate_backup
from tests.conftest import (
    TEST_LABEL_PREFIX,
    requires_docker,
    restic_run,
    start_test_container,
    stop_test_container,
)

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
                        on_complete="echo $DORESTIC_EXIT_CODE > /data/hook_completed",
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
                        on_complete="echo $DORESTIC_EXIT_CODE > /data/hook_completed",
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
                        on_complete="echo $DORESTIC_EXIT_CODE > /data/complete_code",
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
                        on_complete="echo $DORESTIC_EXIT_CODE > /data/complete_code",
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

        container = start_test_container(
            docker_client,
            labels={f"{TEST_LABEL_PREFIX}.enable": "true"},
            binds={data_dir: "/data"},
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
            stop_test_container(container)


# ── per-scope logging ──────────────────────────────────────


@requires_docker
class TestScopeLogging:
    def test_container_ok_logged(
        self, test_container: tuple[Container, Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        container, _ = test_container
        name = container.name or "unknown"
        with patch("dorestic.backup.run_scope_backup", return_value=0):
            with caplog.at_level(logging.INFO):
                backup_container(
                    ContainerTarget(
                        name=name, container=container,
                        container_scope=ScopeConfig(paths=["/data"]),
                    ),
                    config=DUMMY_CONFIG,
                )
        assert "container backup OK" in caplog.text

    def test_container_failed_logged(
        self, test_container: tuple[Container, Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        container, _ = test_container
        name = container.name or "unknown"
        with patch("dorestic.backup.run_scope_backup", return_value=1):
            with caplog.at_level(logging.ERROR):
                backup_container(
                    ContainerTarget(
                        name=name, container=container,
                        container_scope=ScopeConfig(paths=["/data"]),
                    ),
                    config=DUMMY_CONFIG,
                )
        assert "container backup FAILED" in caplog.text

    def test_host_group_ok_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "f.txt").write_text("x")
        with patch("dorestic.backup.run_scope_backup", return_value=0):
            with caplog.at_level(logging.INFO):
                backup_host_group(
                    HostGroup(tag="t", paths=[str(data_dir)]),
                    config=DUMMY_CONFIG,
                )
        assert "backup OK" in caplog.text

    def test_host_group_failed_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "f.txt").write_text("x")
        with patch("dorestic.backup.run_scope_backup", return_value=1):
            with caplog.at_level(logging.ERROR):
                backup_host_group(
                    HostGroup(tag="t", paths=[str(data_dir)]),
                    config=DUMMY_CONFIG,
                )
        assert "backup FAILED" in caplog.text


# ── hostname passed through ────────────────────────────────


@requires_docker
class TestHostnamePassthrough:
    def test_container_scope_passes_hostname(
        self, test_container: tuple[Container, Path],
    ) -> None:
        container, _ = test_container
        name = container.name or "unknown"
        calls: list[dict[str, Any]] = []

        def mock_backup(tag: str, paths: list[Path], exclude: list[str], **kwargs: Any) -> int:
            calls.append({"tag": tag, **kwargs})
            return 0

        with patch("dorestic.backup.run_scope_backup", side_effect=mock_backup):
            backup_container(
                ContainerTarget(
                    name=name, container=container,
                    container_scope=ScopeConfig(paths=["/data"]),
                ),
                config=DUMMY_CONFIG,
            )

        assert len(calls) == 1
        assert "hostname" in calls[0]
        assert calls[0]["hostname"] == make_restic_hostname("container", name)

    def test_host_scope_passes_hostname(
        self, test_container_multi_scope: tuple[Container, Path, Path],
    ) -> None:
        container, _, compose_dir = test_container_multi_scope
        container.reload()
        name = container.name or "unknown"
        calls: list[dict[str, Any]] = []

        def mock_backup(tag: str, paths: list[Path], exclude: list[str], **kwargs: Any) -> int:
            calls.append({"tag": tag, **kwargs})
            return 0

        with patch("dorestic.backup.run_scope_backup", side_effect=mock_backup):
            backup_container(
                ContainerTarget(
                    name=name, container=container,
                    container_scope=ScopeConfig(paths=["/data"]),
                    host_scope=ScopeConfig(paths=[".@1"]),
                    compose_dir=str(compose_dir),
                ),
                config=DUMMY_CONFIG,
            )

        host_call = next(c for c in calls if "host" in c["tag"])
        assert host_call["hostname"] == make_restic_hostname("host", name)

    def test_host_group_passes_hostname(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "f.txt").write_text("x")
        calls: list[dict[str, Any]] = []

        def mock_backup(tag: str, paths: list[Path], exclude: list[str], **kwargs: Any) -> int:
            calls.append({"tag": tag, **kwargs})
            return 0

        with patch("dorestic.backup.run_scope_backup", side_effect=mock_backup):
            backup_host_group(
                HostGroup(tag="documents", paths=[str(data_dir)]),
                config=DUMMY_CONFIG,
            )

        assert len(calls) == 1
        assert calls[0]["hostname"] == "dorestic-host-documents"


# ── hook / resolution ordering ──────────────────────────────


class TestHookOrdering:
    """Regression tests for the ordering bug that silently lost two databases.

    `resolve_container_paths` performs a `docker cp` for any path that is not on
    a mount. When it ran before `container.on_start`, a `pg_dump` hook writing
    /tmp/dump.sql was copied *before* it existed; `on_complete` then deleted it.
    The copy failing left `container_paths` empty, which used to be reported as
    a successful skip.
    """

    def _target(self, **scope: Any) -> ContainerTarget:
        container = MagicMock()
        container.name = "db"
        return ContainerTarget(
            name="db",
            container=container,
            container_scope=ScopeConfig(**scope),
        )

    def test_container_on_start_runs_before_path_resolution(self) -> None:
        order: list[str] = []

        def mock_exec(*_a: Any, **_kw: Any) -> tuple[int, str]:
            order.append("on_start")
            return 0, ""

        def mock_resolve(*_a: Any, **_kw: Any) -> list[Path]:
            order.append("resolve")
            return [Path("/tmp/dump.sql")]

        with (
            patch("dorestic.backup.run_docker_exec", side_effect=mock_exec),
            patch("dorestic.backup.resolve_container_paths", side_effect=mock_resolve),
            patch("dorestic.backup.run_scope_backup", return_value=0),
        ):
            backup_container(
                self._target(paths=["/tmp/dump.sql"], on_start="pg_dump > /tmp/dump.sql"),
                config=DUMMY_CONFIG,
            )

        assert order == ["on_start", "resolve"]

    def test_host_on_start_runs_before_path_resolution(self) -> None:
        order: list[str] = []

        def mock_hook(*_a: Any, **_kw: Any) -> int:
            order.append("on_start")
            return 0

        def mock_resolve(*_a: Any, **_kw: Any) -> list[Path]:
            order.append("resolve")
            return [Path("/data")]

        with (
            patch("dorestic.backup.run_hook", side_effect=mock_hook),
            patch("dorestic.backup.resolve_host_paths", side_effect=mock_resolve),
            patch("dorestic.backup.run_scope_backup", return_value=0),
        ):
            container = MagicMock()
            container.name = "db"
            backup_container(
                ContainerTarget(
                    name="db",
                    container=container,
                    host_scope=ScopeConfig(paths=["."], on_start="make-dump"),
                ),
                config=DUMMY_CONFIG,
            )

        assert order == ["on_start", "resolve"]

    def test_failed_on_start_skips_resolution_entirely(self) -> None:
        """No point running a `docker cp` for a dump the hook never produced."""
        with (
            patch("dorestic.backup.run_docker_exec", return_value=(1, "")),
            patch("dorestic.backup.resolve_container_paths") as mock_resolve,
        ):
            container_result, _ = backup_container(
                self._target(paths=["/tmp/dump.sql"], on_start="false"),
                config=DUMMY_CONFIG,
            )

        mock_resolve.assert_not_called()
        assert container_result.exit_code == EXIT_ON_START_FAILED

    def test_unresolved_container_paths_fail_loudly(self) -> None:
        """The exact production symptom: `docker cp` fails, nothing is backed up.

        This must be an error, not `exit_code=0, skipped=True` — the latter kept
        the healthcheck green while two databases were never backed up at all.
        """
        with (
            patch("dorestic.backup.run_docker_exec", return_value=(0, "")),
            patch("dorestic.backup.resolve_container_paths", return_value=[]),
            patch("dorestic.backup.run_scope_backup", return_value=0) as mock_backup,
        ):
            container_result, _ = backup_container(
                self._target(paths=["/tmp/dump.sql"], on_start="pg_dump > /tmp/dump.sql"),
                config=DUMMY_CONFIG,
            )

        mock_backup.assert_not_called()
        assert container_result.exit_code == EXIT_NO_PATHS_RESOLVED
        assert container_result.skipped is False

    def test_unresolved_host_paths_fail_loudly(self) -> None:
        with (
            patch("dorestic.backup.resolve_host_paths", return_value=[]),
            patch("dorestic.backup.run_scope_backup", return_value=0),
        ):
            container = MagicMock()
            container.name = "db"
            _, host_result = backup_container(
                ContainerTarget(
                    name="db",
                    container=container,
                    host_scope=ScopeConfig(paths=["./config"]),
                ),
                config=DUMMY_CONFIG,
            )

        assert host_result.exit_code == EXIT_NO_PATHS_RESOLVED
        assert host_result.skipped is False

    def test_undeclared_scope_stays_a_silent_skip(self) -> None:
        """Only a *declared* scope that resolves to nothing is an error."""
        container = MagicMock()
        container.name = "db"
        with patch("dorestic.backup.resolve_container_paths", return_value=[]):
            container_result, host_result = backup_container(
                ContainerTarget(name="db", container=container),
                config=DUMMY_CONFIG,
            )

        assert (container_result.exit_code, container_result.skipped) == (0, True)
        assert (host_result.exit_code, host_result.skipped) == (0, True)


class TestHostGroupHookOrdering:
    def test_paths_are_re_resolved_after_on_start(self) -> None:
        """A group hook may be what creates the paths, so resolution follows it."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dump.sql"

            def mock_hook(*_a: Any, **_kw: Any) -> int:
                target.write_text("dump")
                return 0

            calls: list[list[Path]] = []

            def mock_backup(_tag: str, paths: list[Path], *_a: Any, **_kw: Any) -> int:
                calls.append(paths)
                return 0

            with (
                patch("dorestic.backup.run_hook", side_effect=mock_hook),
                patch("dorestic.backup.run_scope_backup", side_effect=mock_backup),
            ):
                result = backup_host_group(
                    HostGroup(
                        tag="dumps",
                        paths=[str(target)],
                        on_start=f"pg_dump > {target}",
                    ),
                    config=DUMMY_CONFIG,
                )

            assert result.exit_code == 0
            assert calls == [[target]]


# ── backup_host_group ───────────────────────────────────────


class TestBackupHostGroup:
    def test_fails_when_no_valid_paths(self) -> None:
        """A declared group whose paths all vanish is a failure, not a no-op.

        Reporting exit 0 here is what let a broken backup ping healthchecks.io
        green for months.
        """
        group = HostGroup(
            tag="missing",
            paths=["/nonexistent/path"],
        )
        result = backup_host_group(group, config=DUMMY_CONFIG)
        assert result.exit_code == EXIT_NO_PATHS_RESOLVED
        assert result.skipped is False

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

        complete_marker = tmp_path / "completed"

        with patch("dorestic.backup.run_scope_backup"):
            group = HostGroup(
                tag="t",
                paths=[str(data_dir)],
                on_start="exit 1",
                on_complete=f"echo $DORESTIC_EXIT_CODE > {complete_marker}",
            )
            backup_host_group(group, config=DUMMY_CONFIG)

        assert complete_marker.exists()
        assert complete_marker.read_text().strip() == str(EXIT_ON_START_FAILED)

    def test_on_complete_receives_exit_code(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "file.txt").write_text("x")

        marker = tmp_path / "complete_code"

        with patch("dorestic.backup.run_scope_backup", return_value=0):
            group = HostGroup(
                tag="t",
                paths=[str(data_dir)],
                on_complete=f"echo $DORESTIC_EXIT_CODE > {marker}",
            )
            backup_host_group(group, config=DUMMY_CONFIG)

        assert marker.exists()
        assert marker.read_text().strip() == "0"

    def test_on_start_success_allows_backup(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "file.txt").write_text("x")

        with patch("dorestic.backup.run_scope_backup", return_value=0) as mock_backup:
            group = HostGroup(
                tag="t",
                paths=[str(data_dir)],
                on_start="exit 0",
            )
            result = backup_host_group(group, config=DUMMY_CONFIG)

        mock_backup.assert_called_once()
        assert result.exit_code == 0

    def test_on_start_receives_tag(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "file.txt").write_text("x")

        marker = tmp_path / "tag_received"

        with patch("dorestic.backup.run_scope_backup", return_value=0):
            group = HostGroup(
                tag="my-tag",
                paths=[str(data_dir)],
                on_start=f"echo $DORESTIC_TAG > {marker}",
            )
            backup_host_group(group, config=DUMMY_CONFIG)

        assert marker.exists()
        assert marker.read_text().strip() == "my-tag"

    def test_on_complete_receives_tag(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "file.txt").write_text("x")

        marker = tmp_path / "tag_received"

        with patch("dorestic.backup.run_scope_backup", return_value=0):
            group = HostGroup(
                tag="my-tag",
                paths=[str(data_dir)],
                on_complete=f"echo $DORESTIC_TAG > {marker}",
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

    def test_snapshot_path_survives_a_changed_source(
        self,
        backup_config: BackupConfig,
        restic_password_file: Path,
        docker_visible_tmp: Path,
    ) -> None:
        """One logical path keeps one snapshot lineage when its source moves.

        A recreated container can report a fresh, instance-scoped source for
        the same directory, and a `docker cp` fallback stages under a new
        mkdtemp every run. Recording either as the snapshot path would start a
        new lineage on each backup — restic groups parent snapshots by
        host+paths — forcing a full rescan and splitting the history.
        """
        repo_path = Path(backup_config.repository)

        first = docker_visible_tmp / "pin-aaaa"
        second = docker_visible_tmp / "pin-bbbb"
        for source in (first, second):
            source.mkdir()
            (source / "db.sql").write_text("dump")

        pinned = stable_mount_target("/data")
        for source in (first, second):
            assert run_scope_backup(
                "myapp:container",
                [BackupPath(source=source, target=pinned, daemon_sourced=True)],
                [],
                config=backup_config,
                hostname=make_restic_hostname("container", "myapp"),
            ) == 0

        result = restic_run(
            "snapshots", "--json", "--tag", "myapp:container",
            repo=repo_path, password_file=restic_password_file,
        )
        snapshots = json.loads(result.stdout)
        assert len(snapshots) == 2
        # Same recorded path both times, and neither source leaks into it.
        assert {tuple(s["paths"]) for s in snapshots} == {(str(pinned),)}
        assert str(first) not in result.stdout
        assert str(second) not in result.stdout

    def test_two_targets_sharing_a_path_stay_separate(
        self,
        backup_config: BackupConfig,
        restic_password_file: Path,
        docker_visible_tmp: Path,
    ) -> None:
        """Pinned targets collide across containers; the hostname keeps them apart.

        Two containers that both back up /data now both record
        /dorestic/data, and restic selects a parent by host and paths — not by
        tag. Were the hostname shared, each target would pick up the other's
        snapshot as its parent and thrash. Every scope therefore passes a
        per-target hostname; this is what makes that load-bearing.
        """
        repo_path = Path(backup_config.repository)
        pinned = stable_mount_target("/data")

        for name in ("alpha", "beta"):
            source = docker_visible_tmp / name
            source.mkdir()
            (source / f"{name}.txt").write_text(name)
            assert run_scope_backup(
                f"{name}:container",
                [BackupPath(source=source, target=pinned, daemon_sourced=True)],
                [],
                config=backup_config,
                hostname=make_restic_hostname("container", name),
            ) == 0

        result = restic_run(
            "snapshots", "--json",
            repo=repo_path, password_file=restic_password_file,
        )
        snapshots = json.loads(result.stdout)
        hostnames = {s["hostname"] for s in snapshots}
        # Same recorded path, distinct parent groups.
        assert {tuple(s["paths"]) for s in snapshots} == {(str(pinned),)}
        assert hostnames == {
            make_restic_hostname("container", "alpha"),
            make_restic_hostname("container", "beta"),
        }

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
        container = start_test_container(
            docker_client,
            labels={f"{TEST_LABEL_PREFIX}.enable": "true"},
            command=(
                "sh -c 'mkdir -p /app/data "
                "&& echo secret > /app/data/file.txt && sleep 3600'"
            ),
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
            stop_test_container(container)


# ── orchestrate_backup / --only filtering ─────────────────


class TestOrchestrateBackup:
    """Test orchestrate_backup directly, mocking Docker and restic."""

    def _make_target(self, name: str) -> ContainerTarget:
        container = MagicMock()
        container.name = name
        return ContainerTarget(name=name, container=container)

    def _run(
        self,
        config: BackupConfig,
        only: str | None = None,
        targets: list[ContainerTarget] | None = None,
        restic_codes: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        if targets is None:
            targets = []
        codes = restic_codes or {}

        backup_calls: list[str] = []
        host_group_calls: list[str] = []
        restic_calls: list[str] = []
        hook_calls: list[str] = []

        def mock_backup_container(target: Any, **_: Any) -> tuple[Any, Any]:
            backup_calls.append(target.name)
            return ScopeResult(exit_code=0), ScopeResult(exit_code=0)

        def mock_backup_host_group(group: Any, **_: Any) -> Any:
            host_group_calls.append(group.tag)
            return ScopeResult(exit_code=0)

        def mock_run_restic(*args: str, **kwargs: Any) -> Any:
            restic_calls.append(args[0])
            if kwargs.get("capture"):
                return codes.get(args[0], 0), "", ""
            return codes.get(args[0], 0)

        def mock_run_hook(command: str, **_: Any) -> int:
            hook_calls.append(command)
            return 0

        with (
            patch("dorestic.backup.docker.DockerClient") as mock_docker,
            patch("dorestic.backup.discover_targets", return_value=targets),
            patch("dorestic.backup.backup_container", side_effect=mock_backup_container),
            patch("dorestic.backup.backup_host_group", side_effect=mock_backup_host_group),
            patch("dorestic.backup.run_restic", side_effect=mock_run_restic),
            patch("dorestic.backup.run_hook", side_effect=mock_run_hook),
            # staging cleanup shells out to `docker run`; without this the test
            # needs a real Docker binary on PATH
            patch("dorestic.backup.docker_rmtree"),
        ):
            mock_docker.from_env.return_value = MagicMock()
            exit_code = orchestrate_backup(config, only=only)

        return {
            "exit_code": exit_code,
            "backup_calls": backup_calls,
            "host_group_calls": host_group_calls,
            "restic_calls": restic_calls,
            "hook_calls": hook_calls,
        }

    def test_only_filters_to_matching_container(self) -> None:
        targets = [self._make_target("db"), self._make_target("redis")]

        result = self._run(DUMMY_CONFIG, only="db", targets=targets)

        assert result["exit_code"] == 0
        assert result["backup_calls"] == ["db"]

    def test_only_filters_to_matching_host_group(self) -> None:
        config = BackupConfig(
            repository="/dummy", password_file="/dummy",
            host_groups=[
                HostGroup(tag="documents", paths=["/data/docs"]),
                HostGroup(tag="photos", paths=["/data/photos"]),
            ],
        )

        result = self._run(config, only="documents")

        assert result["exit_code"] == 0
        assert result["backup_calls"] == []
        assert result["host_group_calls"] == ["documents"]

    def test_only_skips_global_hooks(self) -> None:
        config = BackupConfig(
            repository="/dummy", password_file="/dummy",
            on_start="echo global_start",
            on_complete="echo global_complete",
        )
        targets = [self._make_target("db")]

        result = self._run(config, only="db", targets=targets)

        assert result["exit_code"] == 0
        assert result["hook_calls"] == []

    def test_only_skips_prune_and_check(self) -> None:
        targets = [self._make_target("db")]

        result = self._run(DUMMY_CONFIG, only="db", targets=targets)

        assert "forget" not in result["restic_calls"]
        assert "check" not in result["restic_calls"]

    def test_full_backup_runs_prune_and_check(self) -> None:
        targets = [self._make_target("db")]

        result = self._run(DUMMY_CONFIG, only=None, targets=targets)

        assert "forget" in result["restic_calls"]
        assert "check" in result["restic_calls"]

    def test_full_backup_runs_global_hooks(self) -> None:
        config = BackupConfig(
            repository="/dummy", password_file="/dummy",
            on_start="echo global_start",
            on_complete="echo global_complete",
        )
        targets = [self._make_target("db")]

        result = self._run(config, only=None, targets=targets)

        assert "echo global_start" in result["hook_calls"]
        assert "echo global_complete" in result["hook_calls"]

    def test_only_no_match_returns_error(self) -> None:
        targets = [self._make_target("db")]

        result = self._run(DUMMY_CONFIG, only="nonexistent", targets=targets)

        assert result["exit_code"] == 1
        assert result["backup_calls"] == []

    def test_only_matches_host_group_with_no_containers(self) -> None:
        config = BackupConfig(
            repository="/dummy", password_file="/dummy",
            host_groups=[HostGroup(tag="documents", paths=["/data/docs"])],
        )

        result = self._run(config, only="documents", targets=[])

        assert result["exit_code"] == 0
        assert result["host_group_calls"] == ["documents"]

    def test_failed_prune_is_counted_as_an_error(self) -> None:
        """`forget --prune` exiting non-zero must not leave the run green."""
        result = self._run(
            DUMMY_CONFIG, targets=[self._make_target("db")],
            restic_codes={"forget": 1},
        )

        assert result["exit_code"] != 0

    def test_failed_check_is_counted_as_an_error(self) -> None:
        """A repository that fails `restic check` is a failed backup run."""
        result = self._run(
            DUMMY_CONFIG, targets=[self._make_target("db")],
            restic_codes={"check": 1},
        )

        assert result["exit_code"] != 0

    def test_check_still_runs_after_a_failed_prune(self) -> None:
        result = self._run(
            DUMMY_CONFIG, targets=[self._make_target("db")],
            restic_codes={"forget": 1},
        )

        assert "check" in result["restic_calls"]

    def test_only_matches_both_container_and_host_group(self) -> None:
        config = BackupConfig(
            repository="/dummy", password_file="/dummy",
            host_groups=[HostGroup(tag="myapp", paths=["/data"])],
        )
        targets = [self._make_target("myapp")]

        result = self._run(config, only="myapp", targets=targets)

        assert result["exit_code"] == 0
        assert result["backup_calls"] == ["myapp"]
        assert result["host_group_calls"] == ["myapp"]
