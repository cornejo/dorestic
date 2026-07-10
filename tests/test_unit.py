"""Unit tests for pure functions that don't require Docker or restic."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from dorestic import (
    DEFAULT_CONTAINER_SHELL,
    DEFAULT_STALE_THRESHOLD_HOURS,
    EXIT_ON_START_FAILED,
    BackupConfig,
    BackupResult,
    DiffEntry,
    DiffResult,
    DryRunPlan,
    DryRunScope,
    DryRunTarget,
    HostGroup,
    RepoStats,
    RestoreResult,
    ScopeConfig,
    ScopeResult,
    Snapshot,
    SnapshotFile,
    StatusReport,
    TeeStream,
    VerifyResult,
    acquire_lock,
    expand_depth_limited_path,
    find_config,
    load_config,
    make_restic_hostname,
    parse_comma_list,
    resolve_host_path_spec,
    run_hook,
)
from dorestic.api import Dorestic
from dorestic.cli import write_example_config
from dorestic.config import (
    refresh_config,
    render_config,
    validate_raw_config,
)
from dorestic.display import (
    format_freshness,
    format_size,
    is_stale,
    print_dry_run_plan,
    print_status,
    print_tag_detail,
    print_tag_summary,
)
from dorestic.models import RetentionPolicy, parse_snapshot_time


# ── parse_comma_list ────────────────────────────────────────


class TestParseCommaList:
    def test_basic(self):
        assert parse_comma_list("a,b,c") == ["a", "b", "c"]

    def test_with_spaces(self):
        assert parse_comma_list(" a , b , c ") == ["a", "b", "c"]

    def test_empty_string(self):
        assert parse_comma_list("") == []

    def test_single_item(self):
        assert parse_comma_list("only") == ["only"]

    def test_trailing_comma(self):
        assert parse_comma_list("a,b,") == ["a", "b"]

    def test_leading_comma(self):
        assert parse_comma_list(",a,b") == ["a", "b"]

    def test_multiple_commas(self):
        assert parse_comma_list("a,,b,,,c") == ["a", "b", "c"]

    def test_paths(self):
        assert parse_comma_list("/var/lib/data,/tmp/cache") == [
            "/var/lib/data",
            "/tmp/cache",
        ]

    def test_exclude_patterns(self):
        assert parse_comma_list("*.log,*.tmp,cache/") == [
            "*.log",
            "*.tmp",
            "cache/",
        ]


# ── resolve_host_path_spec ──────────────────────────────────


class TestResolveHostPathSpec:
    def test_simple_relative(self, tmp_path: Path) -> None:
        path, depth = resolve_host_path_spec(str(tmp_path), "subdir")
        expected = (tmp_path / "subdir").resolve()
        assert path == expected
        assert depth is None

    def test_current_dir(self, tmp_path: Path) -> None:
        path, depth = resolve_host_path_spec(str(tmp_path), ".")
        expected = tmp_path.resolve()
        assert path == expected
        assert depth is None

    def test_depth_suffix(self, tmp_path: Path) -> None:
        _path, depth = resolve_host_path_spec(str(tmp_path), ".@1")
        assert depth == 1

    def test_depth_suffix_larger(self, tmp_path: Path) -> None:
        _path, depth = resolve_host_path_spec(str(tmp_path), "../sibling@3")
        assert depth == 3

    def test_parent_dir(self, tmp_path: Path) -> None:
        compose_dir = tmp_path / "project"
        compose_dir.mkdir()
        path, depth = resolve_host_path_spec(str(compose_dir), "../other")
        expected = (compose_dir / "../other").resolve()
        assert path == expected
        assert depth is None

    def test_at_in_dirname_no_depth(self, tmp_path: Path) -> None:
        """@ in the directory name shouldn't be parsed as depth."""
        path, depth = resolve_host_path_spec(str(tmp_path), "dir@name/sub")
        assert depth is None
        assert "dir@name" in str(path)

    def test_zero_depth(self, tmp_path: Path) -> None:
        _path, depth = resolve_host_path_spec(str(tmp_path), ".@0")
        assert depth == 0


# ── expand_depth_limited_path ───────────────────────────────


class TestExpandDepthLimitedPath:
    def test_depth_1(self, tmp_path_with_files: Path) -> None:
        files = expand_depth_limited_path(tmp_path_with_files, 1)
        names = {f.name for f in files}
        assert "file1.txt" in names
        assert "file2.log" in names
        assert "nested.txt" not in names

    def test_depth_2(self, tmp_path_with_files: Path) -> None:
        files = expand_depth_limited_path(tmp_path_with_files, 2)
        names = {f.name for f in files}
        assert "file1.txt" in names
        assert "nested.txt" in names
        assert "level2.txt" not in names

    def test_depth_unlimited(self, tmp_path_with_files: Path) -> None:
        files = expand_depth_limited_path(tmp_path_with_files, 100)
        names = {f.name for f in files}
        assert "level2.txt" in names

    def test_nonexistent_path(self, tmp_path: Path) -> None:
        files = expand_depth_limited_path(tmp_path / "nope", 1)
        assert files == []




# ── make_restic_hostname ───────────────────────────────────


class TestMakeResticHostname:
    def test_basic(self):
        assert make_restic_hostname("container", "immich") == "dorestic-container-immich"

    def test_host_scope(self):
        assert make_restic_hostname("host", "documents") == "dorestic-host-documents"

    def test_special_chars_replaced(self):
        result = make_restic_hostname("container", "my_app.server")
        assert result == "dorestic-container-my-app-server"

    def test_deterministic(self):
        a = make_restic_hostname("container", "test")
        b = make_restic_hostname("container", "test")
        assert a == b

    def test_different_scopes_differ(self):
        a = make_restic_hostname("container", "app")
        b = make_restic_hostname("host", "app")
        assert a != b

    def test_max_length_63(self):
        long_tag = "a" * 100
        result = make_restic_hostname("container", long_tag)
        assert len(result) <= 63

    def test_long_name_has_hash_suffix(self):
        long_tag = "a" * 100
        result = make_restic_hostname("container", long_tag)
        assert len(result) == 63
        assert result[-9] == "-"

    def test_long_name_deterministic(self):
        long_tag = "very-long-container-name-that-exceeds-the-limit-" + "x" * 60
        a = make_restic_hostname("container", long_tag)
        b = make_restic_hostname("container", long_tag)
        assert a == b

    def test_similar_long_names_differ(self):
        base = "x" * 80
        a = make_restic_hostname("container", base + "aaa")
        b = make_restic_hostname("container", base + "bbb")
        assert a != b


# ── run_hook ───────────────────────────────────────────────


class TestRunHook:
    def test_returns_exit_code(self):
        assert run_hook("exit 0") == 0
        assert run_hook("exit 1") == 1

    def test_runs_via_sh(self):
        assert run_hook("echo hello && echo world") == 0

    def test_env_vars_available(self, tmp_path: Path):
        out = tmp_path / "out.txt"
        run_hook(f"echo $DORESTIC_TAG > {out}", env={"DORESTIC_TAG": "myapp"})
        assert out.read_text().strip() == "myapp"

    def test_multiple_env_vars(self, tmp_path: Path):
        out = tmp_path / "out.txt"
        run_hook(
            f'echo "$DORESTIC_TAG $DORESTIC_EXIT_CODE" > {out}',
            env={"DORESTIC_TAG": "db", "DORESTIC_EXIT_CODE": "0"},
        )
        assert out.read_text().strip() == "db 0"

    def test_inherits_parent_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setenv("DORESTIC_TEST_MARKER", "inherited")
        out = tmp_path / "out.txt"
        run_hook(f"echo $DORESTIC_TEST_MARKER > {out}")
        assert out.read_text().strip() == "inherited"


# ── TeeStream ──────────────────────────────────────────────


class TestTeeStream:
    def test_writes_to_both(self):
        original = io.StringIO()
        log_file = io.StringIO()
        tee = TeeStream(original, log_file)

        tee.write("hello")
        assert original.getvalue() == "hello"
        assert log_file.getvalue() == "hello"

    def test_returns_length(self):
        original = io.StringIO()
        log_file = io.StringIO()
        tee = TeeStream(original, log_file)

        assert tee.write("test") == 4

    def test_flush_both(self):
        original = io.StringIO()
        log_file = io.StringIO()
        tee = TeeStream(original, log_file)

        tee.write("data")
        tee.flush()
        assert original.getvalue() == "data"
        assert log_file.getvalue() == "data"

    def test_multiple_writes(self):
        original = io.StringIO()
        log_file = io.StringIO()
        tee = TeeStream(original, log_file)

        tee.write("one ")
        tee.write("two ")
        tee.write("three")
        assert original.getvalue() == "one two three"
        assert log_file.getvalue() == "one two three"


# ── acquire_lock ────────────────────────────────────────────


class TestAcquireLock:
    def _make_config(self, repo: str = "/test/repo") -> "BackupConfig":
        return BackupConfig(repository=repo, password_file="/dummy")

    def test_acquires_lock(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
        config = self._make_config()
        fd = acquire_lock(config)
        fd.close()

    def test_second_lock_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
        config = self._make_config()
        fd1 = acquire_lock(config)

        with pytest.raises(RuntimeError, match="Another backup is already running"):
            acquire_lock(config)

        fd1.close()

    def test_lock_released_on_close(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
        config = self._make_config()
        fd1 = acquire_lock(config)
        fd1.close()

        fd2 = acquire_lock(config)
        fd2.close()

    def test_different_repos_independent(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
        config_a = self._make_config("/repo/a")
        config_b = self._make_config("/repo/b")
        fd_a = acquire_lock(config_a)
        fd_b = acquire_lock(config_b)
        fd_a.close()
        fd_b.close()


# ── ScopeResult / ScopeConfig dataclasses ───────────────────


class TestDataModels:
    def test_scope_result_defaults(self):
        r = ScopeResult(exit_code=0)
        assert r.exit_code == 0
        assert r.skipped is False

    def test_scope_result_skipped(self):
        r = ScopeResult(exit_code=EXIT_ON_START_FAILED, skipped=True)
        assert r.exit_code == EXIT_ON_START_FAILED
        assert r.skipped is True

    def test_scope_config_defaults(self):
        c = ScopeConfig(paths=["/data"])
        assert c.paths == ["/data"]
        assert c.exclude == []
        assert c.on_start is None
        assert c.on_complete is None
        assert c.shell == DEFAULT_CONTAINER_SHELL

    def test_scope_config_custom_shell(self):
        c = ScopeConfig(paths=["/data"], shell="/bin/bash")
        assert c.shell == "/bin/bash"

    def test_scope_config_full(self):
        c = ScopeConfig(
            paths=["/a", "/b"],
            exclude=["*.log"],
            on_start="echo start",
            on_complete="echo done",
        )
        assert len(c.paths) == 2
        assert c.exclude == ["*.log"]

    def test_host_group_defaults(self) -> None:
        g = HostGroup(tag="test", paths=["/data"])
        assert g.exclude == []
        assert g.on_start is None
        assert g.on_complete is None


# ── load_config ────────────────────────────────────────────


class TestLoadConfig:
    def _make_pw_file(self, tmp_path: Path) -> Path:
        pw = tmp_path / "restic-pw"
        pw.write_text("test-password")
        return pw

    def test_minimal(self, tmp_path: Path) -> None:
        pw = self._make_pw_file(tmp_path)
        config_file = tmp_path / "config.yml"
        config_file.write_text(yaml.dump({
            "repository": "/mnt/backup",
            "password_file": str(pw),
        }))

        cfg = load_config(str(config_file))
        assert cfg.repository == "/mnt/backup"
        assert cfg.password_file == str(pw)
        assert cfg.restic_image == "restic/restic:latest"
        assert cfg.on_start is None
        assert cfg.on_complete is None
        assert cfg.retention.daily == 7
        assert cfg.retention.weekly == 4
        assert cfg.retention.monthly == 12
        assert cfg.host_groups == []

    def test_full(self, tmp_path: Path) -> None:
        pw = self._make_pw_file(tmp_path)
        config_file = tmp_path / "config.yml"
        config_file.write_text(yaml.dump({
            "repository": "/backup",
            "password_file": str(pw),
            "restic_image": "restic/restic:0.16",
            "on_start": "/scripts/start.sh",
            "on_complete": "/scripts/done.sh",
            "retention": {"daily": 14, "weekly": 8, "monthly": 24},
            "host_groups": [
                {
                    "tag": "docs",
                    "paths": ["/data/docs"],
                    "exclude": ["*.tmp"],
                    "on_start": "/scripts/pre.sh",
                    "on_complete": "/scripts/post.sh",
                },
            ],
        }))

        cfg = load_config(str(config_file))
        assert cfg.restic_image == "restic/restic:0.16"
        assert cfg.on_start == "/scripts/start.sh"
        assert cfg.on_complete == "/scripts/done.sh"
        assert cfg.retention.daily == 14
        assert cfg.retention.weekly == 8
        assert cfg.retention.monthly == 24
        assert len(cfg.host_groups) == 1
        assert cfg.host_groups[0].tag == "docs"
        assert cfg.host_groups[0].paths == ["/data/docs"]
        assert cfg.host_groups[0].exclude == ["*.tmp"]
        assert cfg.host_groups[0].on_start == "/scripts/pre.sh"
        assert cfg.host_groups[0].on_complete == "/scripts/post.sh"

    def test_invalid_format(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yml"
        config_file.write_text("just a string")

        with pytest.raises(ValueError, match="must be a YAML mapping"):
            load_config(str(config_file))

    def test_custom_retention(self, tmp_path: Path) -> None:
        pw = self._make_pw_file(tmp_path)
        config_file = tmp_path / "config.yml"
        config_file.write_text(yaml.dump({
            "repository": "/backup",
            "password_file": str(pw),
            "retention": {"daily": 30},
        }))

        cfg = load_config(str(config_file))
        assert cfg.retention.daily == 30
        assert cfg.retention.weekly == 4
        assert cfg.retention.monthly == 12

    def test_multiple_host_groups(self, tmp_path: Path) -> None:
        pw = self._make_pw_file(tmp_path)
        config_file = tmp_path / "config.yml"
        config_file.write_text(yaml.dump({
            "repository": "/backup",
            "password_file": str(pw),
            "host_groups": [
                {"tag": "a", "paths": ["/a"]},
                {"tag": "b", "paths": ["/b"], "exclude": ["*.log"]},
            ],
        }))

        cfg = load_config(str(config_file))
        assert len(cfg.host_groups) == 2
        assert cfg.host_groups[0].tag == "a"
        assert cfg.host_groups[1].exclude == ["*.log"]

    def test_missing_repository(self, tmp_path: Path) -> None:
        pw = self._make_pw_file(tmp_path)
        config_file = tmp_path / "config.yml"
        config_file.write_text(yaml.dump({"password_file": str(pw)}))

        with pytest.raises(ValueError, match="missing required field 'repository'"):
            load_config(str(config_file))

    def test_missing_password_file(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yml"
        config_file.write_text(yaml.dump({"repository": "/backup"}))

        with pytest.raises(ValueError, match="missing required field 'password_file'"):
            load_config(str(config_file))

    def test_password_file_not_on_disk(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yml"
        config_file.write_text(yaml.dump({
            "repository": "/backup",
            "password_file": "/nonexistent/restic-pw",
        }))

        with pytest.raises(ValueError, match="does not exist"):
            load_config(str(config_file))

    def test_excludes_typo_root(self, tmp_path: Path) -> None:
        pw = self._make_pw_file(tmp_path)
        config_file = tmp_path / "config.yml"
        config_file.write_text(yaml.dump({
            "repository": "/backup",
            "password_file": str(pw),
            "excludes": ["*.tmp"],
        }))

        with pytest.raises(ValueError, match="excludes.*plural"):
            load_config(str(config_file))

    def test_excludes_typo_host_group(self, tmp_path: Path) -> None:
        pw = self._make_pw_file(tmp_path)
        config_file = tmp_path / "config.yml"
        config_file.write_text(yaml.dump({
            "repository": "/backup",
            "password_file": str(pw),
            "host_groups": [
                {"tag": "docs", "paths": ["/data"], "excludes": ["*.tmp"]},
            ],
        }))

        with pytest.raises(ValueError, match="excludes.*plural"):
            load_config(str(config_file))


# ── find_config ────────────────────────────────────────────


class TestFindConfig:
    def test_finds_local_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yml").write_text("repository: /backup")

        assert find_config() == "config.yml"

    def test_finds_xdg_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        xdg_dir = tmp_path / "xdg_config" / "dorestic"
        xdg_dir.mkdir(parents=True)
        (xdg_dir / "config.yml").write_text("repository: /backup")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))

        assert find_config() == str(xdg_dir / "config.yml")

    def test_local_takes_precedence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yml").write_text("local")
        xdg_dir = tmp_path / "xdg_config" / "dorestic"
        xdg_dir.mkdir(parents=True)
        (xdg_dir / "config.yml").write_text("xdg")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))

        assert find_config() == "config.yml"

    def test_raises_when_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))

        with pytest.raises(FileNotFoundError, match="No config.yml found"):
            find_config()

    def test_falls_back_to_home_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        config_dir = tmp_path / ".config" / "dorestic"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yml").write_text("repository: /backup")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        assert find_config() == str(config_dir / "config.yml")


# ── CLI --init ─────────────────────────────────────────────


class TestCliInit:
    def test_writes_to_directory(self, tmp_path: Path) -> None:
        write_example_config(str(tmp_path))

        config = tmp_path / "config.yml"
        assert config.exists()
        assert "repository" in config.read_text()

    def test_writes_to_file_path(self, tmp_path: Path) -> None:
        dest = tmp_path / "my-config.yml"
        write_example_config(str(dest))

        assert dest.exists()
        assert "password_file" in dest.read_text()

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        dest = tmp_path / "nested" / "dir" / "config.yml"
        write_example_config(str(dest))

        assert dest.exists()

    def test_refuses_to_overwrite(self, tmp_path: Path) -> None:
        existing = tmp_path / "config.yml"
        existing.write_text("existing")

        with pytest.raises(SystemExit):
            write_example_config(str(tmp_path))


# ── format_freshness ─────────────────────────────────────


class TestFormatFreshness:
    def _now(self) -> datetime:
        return datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)

    def test_just_now(self) -> None:
        now = self._now()
        assert format_freshness(now, now) == "just now"

    def test_minutes(self) -> None:
        now = self._now()
        dt = datetime(2026, 7, 9, 11, 45, 0, tzinfo=timezone.utc)
        assert format_freshness(dt, now) == "15m ago"

    def test_hours(self) -> None:
        now = self._now()
        dt = datetime(2026, 7, 9, 6, 0, 0, tzinfo=timezone.utc)
        assert format_freshness(dt, now) == "6h ago"

    def test_one_day(self) -> None:
        now = self._now()
        dt = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
        assert format_freshness(dt, now) == "1d ago"

    def test_multiple_days(self) -> None:
        now = self._now()
        dt = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)
        assert format_freshness(dt, now) == "3d ago"

    def test_future_shows_just_now(self) -> None:
        now = self._now()
        dt = datetime(2026, 7, 9, 13, 0, 0, tzinfo=timezone.utc)
        assert format_freshness(dt, now) == "just now"


# ── is_stale ─────────────────────────────────────────────


class TestIsStale:
    def _now(self) -> datetime:
        return datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)

    def test_within_threshold(self) -> None:
        now = self._now()
        dt = datetime(2026, 7, 9, 2, 0, 0, tzinfo=timezone.utc)
        assert is_stale(dt, now, 25) is False

    def test_at_threshold(self) -> None:
        now = self._now()
        dt = datetime(2026, 7, 8, 11, 0, 0, tzinfo=timezone.utc)
        assert is_stale(dt, now, 25) is True

    def test_beyond_threshold(self) -> None:
        now = self._now()
        dt = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)
        assert is_stale(dt, now, 25) is True

    def test_custom_threshold(self) -> None:
        now = self._now()
        dt = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
        assert is_stale(dt, now, 48) is False
        assert is_stale(dt, now, 24) is True


# ── parse_snapshot_time ──────────────────────────────────


class TestParseSnapshotTime:
    def test_basic_iso(self) -> None:
        dt = parse_snapshot_time("2026-07-09T02:00:00")
        assert dt.year == 2026
        assert dt.month == 7
        assert dt.hour == 2
        assert dt.tzinfo == timezone.utc

    def test_with_z_suffix(self) -> None:
        dt = parse_snapshot_time("2026-07-09T02:00:00Z")
        assert dt.tzinfo == timezone.utc

    def test_with_nanosecond_fraction(self) -> None:
        dt = parse_snapshot_time("2026-07-09T02:00:00.123456789")
        assert dt.microsecond == 123456

    def test_with_fraction_and_z(self) -> None:
        dt = parse_snapshot_time("2026-07-09T02:00:00.123456789Z")
        assert dt.microsecond == 123456
        assert dt.tzinfo == timezone.utc


# ── format_size ──────────────────────────────────────────


class TestFormatSize:
    def test_bytes(self) -> None:
        assert format_size(500) == "500 B"

    def test_kib(self) -> None:
        assert format_size(2048) == "2.0 KiB"

    def test_mib(self) -> None:
        assert format_size(5 * 1024 * 1024) == "5.0 MiB"

    def test_gib(self) -> None:
        assert format_size(3 * 1024 * 1024 * 1024) == "3.0 GiB"

    def test_zero(self) -> None:
        assert format_size(0) == "0 B"


# ── stale_threshold_hours config ──────────────────────────


class TestStaleThresholdConfig:
    def _make_pw_file(self, tmp_path: Path) -> Path:
        pw = tmp_path / "restic-pw"
        pw.write_text("test-password")
        return pw

    def test_default(self, tmp_path: Path) -> None:
        pw = self._make_pw_file(tmp_path)
        config_file = tmp_path / "config.yml"
        config_file.write_text(yaml.dump({
            "repository": "/backup",
            "password_file": str(pw),
        }))
        cfg = load_config(str(config_file))
        assert cfg.stale_threshold_hours == DEFAULT_STALE_THRESHOLD_HOURS

    def test_custom(self, tmp_path: Path) -> None:
        pw = self._make_pw_file(tmp_path)
        config_file = tmp_path / "config.yml"
        config_file.write_text(yaml.dump({
            "repository": "/backup",
            "password_file": str(pw),
            "stale_threshold_hours": 48,
        }))
        cfg = load_config(str(config_file))
        assert cfg.stale_threshold_hours == 48


# ── Snapshot model ───────────────────────────────────────────


class TestSnapshot:
    def test_from_restic(self) -> None:
        data = {
            "id": "abc123def456",
            "short_id": "abc123de",
            "time": "2026-07-09T02:00:00.123456789Z",
            "tags": ["my-db:container"],
            "paths": ["/var/lib/postgresql/data"],
            "hostname": "dorestic-container-my-db",
        }
        snap = Snapshot.from_restic(data)
        assert snap.id == "abc123def456"
        assert snap.short_id == "abc123de"
        assert snap.time.year == 2026
        assert snap.time.month == 7
        assert snap.tags == ["my-db:container"]
        assert snap.paths == ["/var/lib/postgresql/data"]
        assert snap.hostname == "dorestic-container-my-db"

    def test_from_restic_missing_optional_fields(self) -> None:
        data = {
            "id": "abc123def456",
            "time": "2026-07-09T02:00:00Z",
        }
        snap = Snapshot.from_restic(data)
        assert snap.short_id == "abc123de"
        assert snap.tags == []
        assert snap.paths == []
        assert snap.hostname == ""

    def test_from_restic_null_tags(self) -> None:
        data = {
            "id": "abc123def456",
            "time": "2026-07-09T02:00:00Z",
            "tags": None,
        }
        snap = Snapshot.from_restic(data)
        assert snap.tags == []


# ── SnapshotFile model ───────────────────────────────────────


class TestSnapshotFile:
    def test_from_restic_file(self) -> None:
        data = {"path": "/data/dump.sql", "type": "file", "size": 1048576}
        f = SnapshotFile.from_restic(data)
        assert f.path == "/data/dump.sql"
        assert f.type == "file"
        assert f.size == 1048576

    def test_from_restic_dir(self) -> None:
        data = {"path": "/data", "type": "dir"}
        f = SnapshotFile.from_restic(data)
        assert f.path == "/data"
        assert f.type == "dir"
        assert f.size == 0

    def test_from_restic_empty(self) -> None:
        f = SnapshotFile.from_restic({})
        assert f.path == ""
        assert f.type == ""
        assert f.size == 0


# ── BackupResult model ───────────────────────────────────────


class TestBackupResult:
    def test_success(self) -> None:
        r = BackupResult(success=True)
        assert r.success is True

    def test_failure(self) -> None:
        r = BackupResult(success=False)
        assert r.success is False


# ── Dorestic class ───────────────────────────────────────────


class TestDorestic:
    def test_init_from_config(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        assert d.config is config

    def test_from_config_path(self, tmp_path: Path) -> None:
        pw = tmp_path / "pw"
        pw.write_text("secret")
        config_file = tmp_path / "config.yml"
        config_file.write_text(
            f"repository: /repo\npassword_file: {pw}\n"
        )
        d = Dorestic.from_config_path(str(config_file))
        assert d.config.repository == "/repo"

    def test_list_snapshots(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        raw = [
            {"id": "aaa111", "time": "2026-07-09T02:00:00Z", "tags": ["db"]},
            {"id": "bbb222", "time": "2026-07-08T02:00:00Z", "tags": ["app"]},
        ]
        with patch("dorestic.api.list_snapshots", return_value=raw):
            snaps = d.list_snapshots()
        assert len(snaps) == 2
        assert isinstance(snaps[0], Snapshot)
        assert snaps[0].id == "aaa111"
        assert snaps[1].tags == ["app"]

    def test_list_snapshots_with_tag_filter(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        raw = [{"id": "aaa111", "time": "2026-07-09T02:00:00Z", "tags": ["db"]}]
        with patch("dorestic.api.list_snapshots", return_value=raw) as mock:
            d.list_snapshots(tag="db")
        mock.assert_called_once_with(config, tag="db")

    def test_resolve_snapshot_by_id(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        raw = [
            {"id": "aaa111bbb222", "short_id": "aaa111bb", "time": "2026-07-09T02:00:00Z", "tags": ["db"]},
        ]
        with patch("dorestic.api.list_snapshots", return_value=raw):
            snap = d.resolve_snapshot("aaa111bb")
        assert snap is not None
        assert snap.id == "aaa111bbb222"

    def test_resolve_snapshot_by_tag_picks_latest(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        raw = [
            {"id": "old111", "time": "2026-07-07T02:00:00Z", "tags": ["db"]},
            {"id": "new222", "time": "2026-07-09T02:00:00Z", "tags": ["db"]},
        ]
        with patch("dorestic.api.list_snapshots", return_value=raw):
            snap = d.resolve_snapshot("db")
        assert snap is not None
        assert snap.id == "new222"

    def test_resolve_snapshot_not_found(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        with patch("dorestic.api.list_snapshots", return_value=[]):
            snap = d.resolve_snapshot("nonexistent")
        assert snap is None


# ── validate_raw_config ──────────────────────────────────────


class TestValidateRawConfig:
    def test_valid_minimal(self) -> None:
        validate_raw_config({"repository": "/r", "password_file": "/p"})

    def test_valid_full(self) -> None:
        validate_raw_config({
            "repository": "/r",
            "password_file": "/p",
            "restic_image": "restic/restic:latest",
            "on_start": "echo start",
            "on_complete": "echo done",
            "retention": {"daily": 7, "weekly": 4, "monthly": 12},
            "stale_threshold_hours": 25,
            "host_groups": [
                {"tag": "docs", "paths": ["/data"], "exclude": ["*.tmp"],
                 "on_start": "echo pre", "on_complete": "echo post"},
            ],
        })

    def test_unknown_top_level_key(self) -> None:
        with pytest.raises(ValueError, match="Unknown config keys: timeout"):
            validate_raw_config({
                "repository": "/r", "password_file": "/p", "timeout": 30,
            })

    def test_unknown_retention_key(self) -> None:
        with pytest.raises(ValueError, match="Unknown retention keys: yearly"):
            validate_raw_config({
                "repository": "/r", "password_file": "/p",
                "retention": {"daily": 7, "yearly": 1},
            })

    def test_unknown_host_group_key(self) -> None:
        with pytest.raises(ValueError, match="Unknown keys in host group 'docs': priority"):
            validate_raw_config({
                "repository": "/r", "password_file": "/p",
                "host_groups": [
                    {"tag": "docs", "paths": ["/d"], "priority": "high"},
                ],
            })

    def test_multiple_unknown_keys(self) -> None:
        with pytest.raises(ValueError, match="Unknown config keys:"):
            validate_raw_config({
                "repository": "/r", "password_file": "/p",
                "debug": True, "timeout": 30,
            })


# ── render_config ────────────────────────────────────────────


class TestRenderConfig:
    def test_minimal(self) -> None:
        result = render_config({"repository": "/backup", "password_file": "/pw"})
        assert "repository: /backup" in result
        assert "password_file: /pw" in result
        assert "# restic_image:" in result
        assert "# on_start:" in result
        assert "# retention:" in result

    def test_with_optional_values(self) -> None:
        result = render_config({
            "repository": "/backup",
            "password_file": "/pw",
            "restic_image": "restic/restic:0.16",
            "on_start": "/scripts/start.sh",
            "stale_threshold_hours": 48,
        })
        assert "restic_image: restic/restic:0.16" in result
        assert "on_start: /scripts/start.sh" in result
        assert "stale_threshold_hours: 48" in result

    def test_with_retention(self) -> None:
        result = render_config({
            "repository": "/backup",
            "password_file": "/pw",
            "retention": {"daily": 14, "weekly": 8, "monthly": 24},
        })
        assert "retention:" in result
        assert "  daily: 14" in result
        assert "  weekly: 8" in result
        assert "  monthly: 24" in result

    def test_with_host_groups(self) -> None:
        result = render_config({
            "repository": "/backup",
            "password_file": "/pw",
            "host_groups": [
                {
                    "tag": "docs",
                    "paths": ["/mnt/docs", "/mnt/photos"],
                    "exclude": ["*.tmp"],
                    "on_start": "/pre.sh",
                },
            ],
        })
        assert "host_groups:" in result
        assert "  - tag: docs" in result
        assert "      - /mnt/docs" in result
        assert "      - /mnt/photos" in result
        assert "      - '*.tmp'" in result
        assert "    on_start: /pre.sh" in result

    def test_roundtrip_through_yaml(self) -> None:
        original = {
            "repository": "/backup",
            "password_file": "/pw",
            "restic_image": "restic/restic:0.16",
            "retention": {"daily": 14},
            "host_groups": [{"tag": "docs", "paths": ["/data"]}],
        }
        rendered = render_config(original)
        reparsed = yaml.safe_load(rendered)
        assert reparsed["repository"] == "/backup"
        assert reparsed["restic_image"] == "restic/restic:0.16"
        assert reparsed["retention"]["daily"] == 14
        assert reparsed["host_groups"][0]["tag"] == "docs"

    def test_includes_comments(self) -> None:
        result = render_config({"repository": "/r", "password_file": "/p"})
        assert "# Restic backup configuration" in result
        assert "# Path to the restic repository" in result
        assert "RESTIC_PASSWORD_FILE" in result


# ── refresh_config ───────────────────────────────────────────


class TestRefreshConfig:
    def test_refreshes_config(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yml"
        config.write_text(
            "repository: /backup\npassword_file: /pw\nstale_threshold_hours: 48\n"
        )

        bak_path = refresh_config(str(config))

        assert Path(bak_path).exists()
        assert Path(bak_path).suffix == ".bak"

        new_content = config.read_text()
        assert "repository: /backup" in new_content
        assert "stale_threshold_hours: 48" in new_content
        assert "# Restic backup configuration" in new_content

    def test_bak_contains_old_content(self, tmp_path: Path) -> None:
        old_content = "# my old config\nrepository: /old\npassword_file: /pw\n"
        config = tmp_path / "config.yml"
        config.write_text(old_content)

        bak_path = refresh_config(str(config))

        assert Path(bak_path).read_text() == old_content

    def test_replaces_existing_bak(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yml"
        config.write_text("repository: /r\npassword_file: /p\n")
        bak = tmp_path / "config.yml.bak"
        bak.write_text("ancient backup")

        refresh_config(str(config))

        assert bak.read_text() != "ancient backup"
        assert "repository: /r" in bak.read_text()

    def test_errors_on_unknown_keys(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yml"
        config.write_text(
            "repository: /r\npassword_file: /p\ntimeout: 30\n"
        )

        with pytest.raises(ValueError, match="Unknown config keys: timeout"):
            refresh_config(str(config))

        assert config.read_text().startswith("repository:")

    def test_preserves_host_groups(self, tmp_path: Path) -> None:
        config = tmp_path / "config.yml"
        config.write_text(yaml.dump({
            "repository": "/backup",
            "password_file": "/pw",
            "host_groups": [
                {"tag": "docs", "paths": ["/data"], "exclude": ["*.tmp"]},
            ],
        }))

        refresh_config(str(config))

        reparsed = yaml.safe_load(config.read_text())
        assert len(reparsed["host_groups"]) == 1
        assert reparsed["host_groups"][0]["tag"] == "docs"
        assert reparsed["host_groups"][0]["paths"] == ["/data"]
        assert reparsed["host_groups"][0]["exclude"] == ["*.tmp"]


# ── dry run models ──────────────────────────────────────────


class TestDryRunModels:
    def test_dry_run_scope(self) -> None:
        scope = DryRunScope(
            tag="my-db:container",
            paths=["/srv/data"],
            exclude=["*.log"],
            on_start="pg_dump",
            on_complete="rm dump",
        )
        assert scope.tag == "my-db:container"
        assert scope.paths == ["/srv/data"]
        assert scope.exclude == ["*.log"]

    def test_dry_run_scope_defaults(self) -> None:
        scope = DryRunScope(tag="t", paths=[], exclude=[])
        assert scope.on_start is None
        assert scope.on_complete is None

    def test_dry_run_target(self) -> None:
        cs = DryRunScope(tag="db:container", paths=["/data"], exclude=[])
        hs = DryRunScope(tag="db:host", paths=["/compose"], exclude=[])
        target = DryRunTarget(name="db", container_scope=cs, host_scope=hs)
        assert target.container_scope is cs
        assert target.host_scope is hs

    def test_dry_run_target_no_scopes(self) -> None:
        target = DryRunTarget(name="db")
        assert target.container_scope is None
        assert target.host_scope is None

    def test_dry_run_plan(self) -> None:
        plan = DryRunPlan(
            targets=[DryRunTarget(name="db")],
            host_groups=[DryRunScope(tag="docs", paths=["/data"], exclude=[])],
            global_on_start="pre.sh",
            global_on_complete="post.sh",
        )
        assert len(plan.targets) == 1
        assert len(plan.host_groups) == 1
        assert plan.global_on_start == "pre.sh"

    def test_dry_run_plan_defaults(self) -> None:
        plan = DryRunPlan(targets=[], host_groups=[])
        assert plan.global_on_start is None
        assert plan.global_on_complete is None


# ── print_dry_run_plan ──────────────────────────────────────


class TestPrintDryRunPlan:
    def test_empty_plan(self, capsys: pytest.CaptureFixture[str]) -> None:
        plan = DryRunPlan(targets=[], host_groups=[])
        print_dry_run_plan(plan)
        assert "Nothing to back up." in capsys.readouterr().out

    def test_container_with_both_scopes(self, capsys: pytest.CaptureFixture[str]) -> None:
        plan = DryRunPlan(
            targets=[DryRunTarget(
                name="my-db",
                container_scope=DryRunScope(
                    tag="my-db:container",
                    paths=["/srv/pgdata"],
                    exclude=["*.log"],
                    on_start="pg_dump",
                ),
                host_scope=DryRunScope(
                    tag="my-db:host",
                    paths=["/srv/compose/docker-compose.yml"],
                    exclude=[],
                ),
            )],
            host_groups=[],
        )
        print_dry_run_plan(plan)
        out = capsys.readouterr().out
        assert "my-db" in out
        assert "container (my-db:container)" in out
        assert "/srv/pgdata" in out
        assert "exclude: *.log" in out
        assert "on_start: pg_dump" in out
        assert "host (my-db:host)" in out
        assert "/srv/compose/docker-compose.yml" in out

    def test_host_group(self, capsys: pytest.CaptureFixture[str]) -> None:
        plan = DryRunPlan(
            targets=[],
            host_groups=[DryRunScope(
                tag="docs",
                paths=["/mnt/share"],
                exclude=["*.tmp"],
                on_complete="notify.sh",
            )],
        )
        print_dry_run_plan(plan)
        out = capsys.readouterr().out
        assert "host:docs" in out
        assert "/mnt/share" in out
        assert "exclude: *.tmp" in out
        assert "on_complete: notify.sh" in out

    def test_global_hooks(self, capsys: pytest.CaptureFixture[str]) -> None:
        plan = DryRunPlan(
            targets=[DryRunTarget(name="x", container_scope=DryRunScope(
                tag="x:container", paths=["/data"], exclude=[],
            ))],
            host_groups=[],
            global_on_start="pre.sh",
            global_on_complete="post.sh",
        )
        print_dry_run_plan(plan)
        out = capsys.readouterr().out
        assert "global on_start: pre.sh" in out
        assert "global on_complete: post.sh" in out

    def test_no_paths_resolved(self, capsys: pytest.CaptureFixture[str]) -> None:
        plan = DryRunPlan(
            targets=[DryRunTarget(
                name="empty",
                container_scope=DryRunScope(tag="empty:container", paths=[], exclude=[]),
            )],
            host_groups=[],
        )
        print_dry_run_plan(plan)
        out = capsys.readouterr().out
        assert "(no paths resolved)" in out


# ── log_dir config ──────────────────────────────────────────


class TestLogDirConfig:
    def _make_pw_file(self, tmp_path: Path) -> Path:
        pw = tmp_path / "restic-pw"
        pw.write_text("test-password")
        return pw

    def test_default_none(self, tmp_path: Path) -> None:
        pw = self._make_pw_file(tmp_path)
        config_file = tmp_path / "config.yml"
        config_file.write_text(yaml.dump({
            "repository": "/backup",
            "password_file": str(pw),
        }))
        cfg = load_config(str(config_file))
        assert cfg.log_dir is None

    def test_loads_log_dir(self, tmp_path: Path) -> None:
        pw = self._make_pw_file(tmp_path)
        config_file = tmp_path / "config.yml"
        config_file.write_text(yaml.dump({
            "repository": "/backup",
            "password_file": str(pw),
            "log_dir": "/var/log/dorestic",
        }))
        cfg = load_config(str(config_file))
        assert cfg.log_dir == "/var/log/dorestic"

    def test_render_config_without_log_dir(self) -> None:
        output = render_config({"repository": "/r", "password_file": "/p"})
        assert "# log_dir: /var/log/dorestic" in output

    def test_render_config_with_log_dir(self) -> None:
        output = render_config({
            "repository": "/r",
            "password_file": "/p",
            "log_dir": "/my/logs",
        })
        assert "log_dir: /my/logs" in output
        assert "# log_dir:" not in output

    def test_validate_accepts_log_dir(self) -> None:
        validate_raw_config({
            "repository": "/r",
            "password_file": "/p",
            "log_dir": "/var/log/dorestic",
        })


# ── make_log_path ───────────────────────────────────────────


class TestMakeLogPath:
    def test_persistent_log_dir(self, tmp_path: Path) -> None:
        from dorestic.backup import make_log_path
        config = BackupConfig(
            repository="/repo", password_file="/pw",
            log_dir=str(tmp_path / "logs"),
        )
        path, persistent = make_log_path(config)
        assert persistent is True
        assert path.startswith(str(tmp_path / "logs"))
        assert "backup-" in path
        assert path.endswith(".log")
        assert Path(path).parent.exists()

    def test_temp_log_without_log_dir(self) -> None:
        from dorestic.backup import make_log_path
        config = BackupConfig(repository="/repo", password_file="/pw")
        path, persistent = make_log_path(config)
        assert persistent is False
        assert "backup-" in path
        assert path.endswith(".log")
        Path(path).unlink(missing_ok=True)


# ── RepoStats / StatusReport models ────────────────────────


class TestStatusModels:
    def test_repo_stats(self) -> None:
        stats = RepoStats(total_size=1024000, total_file_count=42)
        assert stats.total_size == 1024000
        assert stats.total_file_count == 42

    def test_status_report(self) -> None:
        snap = Snapshot(
            id="abc123", short_id="abc123", tags=["db:container"],
            time=datetime(2026, 7, 9, 2, 0, 0, tzinfo=timezone.utc),
            paths=["/data"], hostname="host",
        )
        report = StatusReport(
            repository="/backup",
            retention=RetentionPolicy(),
            repo_stats=RepoStats(total_size=5000, total_file_count=10),
            snapshots=[snap],
            stale_threshold_hours=25,
            log_dir="/var/log/dorestic",
        )
        assert report.repository == "/backup"
        assert report.repo_stats is not None
        assert report.repo_stats.total_size == 5000
        assert len(report.snapshots) == 1
        assert report.log_dir == "/var/log/dorestic"

    def test_status_report_no_stats(self) -> None:
        report = StatusReport(
            repository="/backup",
            retention=RetentionPolicy(),
            repo_stats=None,
            snapshots=[],
            stale_threshold_hours=25,
            log_dir=None,
        )
        assert report.repo_stats is None
        assert report.log_dir is None


# ── print_status ────────────────────────────────────────────


class TestPrintStatus:
    def test_with_stats_and_snapshots(self, capsys: pytest.CaptureFixture[str]) -> None:
        snap = Snapshot(
            id="abc123", short_id="abc123", tags=["db:container"],
            time=datetime(2026, 7, 9, 2, 0, 0, tzinfo=timezone.utc),
            paths=["/data"], hostname="host",
        )
        report = StatusReport(
            repository="/mnt/backup",
            retention=RetentionPolicy(daily=7, weekly=4, monthly=12),
            repo_stats=RepoStats(total_size=1024 * 1024 * 50, total_file_count=1234),
            snapshots=[snap],
            stale_threshold_hours=25,
            log_dir=None,
        )
        now = datetime(2026, 7, 9, 11, 0, 0, tzinfo=timezone.utc)
        print_status(report, now)
        out = capsys.readouterr().out
        assert "Repository: /mnt/backup" in out
        assert "50.0 MiB" in out
        assert "1,234" in out
        assert "7 daily" in out
        assert "db:container" in out
        assert "9h ago" in out

    def test_no_snapshots(self, capsys: pytest.CaptureFixture[str]) -> None:
        report = StatusReport(
            repository="/backup",
            retention=RetentionPolicy(),
            repo_stats=None,
            snapshots=[],
            stale_threshold_hours=25,
            log_dir=None,
        )
        now = datetime(2026, 7, 9, 11, 0, 0, tzinfo=timezone.utc)
        print_status(report, now)
        out = capsys.readouterr().out
        assert "No snapshots found." in out

    def test_with_log_dir(self, capsys: pytest.CaptureFixture[str]) -> None:
        report = StatusReport(
            repository="/backup",
            retention=RetentionPolicy(),
            repo_stats=None,
            snapshots=[],
            stale_threshold_hours=25,
            log_dir="/var/log/dorestic",
        )
        now = datetime(2026, 7, 9, 11, 0, 0, tzinfo=timezone.utc)
        print_status(report, now)
        out = capsys.readouterr().out
        assert "Log dir:    /var/log/dorestic" in out

    def test_stale_marker(self, capsys: pytest.CaptureFixture[str]) -> None:
        snap = Snapshot(
            id="abc123", short_id="abc123", tags=["old:container"],
            time=datetime(2026, 7, 6, 2, 0, 0, tzinfo=timezone.utc),
            paths=["/data"], hostname="host",
        )
        report = StatusReport(
            repository="/backup",
            retention=RetentionPolicy(),
            repo_stats=None,
            snapshots=[snap],
            stale_threshold_hours=25,
            log_dir=None,
        )
        now = datetime(2026, 7, 9, 11, 0, 0, tzinfo=timezone.utc)
        print_status(report, now)
        out = capsys.readouterr().out
        assert "(!)" in out


# ── Dorestic.validate ──────────────────────────────────────


class TestDoresticValidate:
    def test_warns_missing_log_dir(self) -> None:
        config = BackupConfig(
            repository="/repo", password_file="/pw",
            log_dir="/nonexistent/path",
        )
        d = Dorestic(config)
        issues = d.validate()
        assert any("log_dir does not exist" in i for i in issues)

    def test_warns_log_dir_not_directory(self, tmp_path: Path) -> None:
        f = tmp_path / "not_a_dir"
        f.write_text("x")
        config = BackupConfig(
            repository="/repo", password_file="/pw",
            log_dir=str(f),
        )
        d = Dorestic(config)
        issues = d.validate()
        assert any("not a directory" in i for i in issues)

    def test_no_issues_without_log_dir(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        issues = d.validate()
        docker_issues = [i for i in issues if "Docker" not in i]
        assert docker_issues == []


# ── restore/verify/diff models ──────────────────────────────


class TestRestoreResult:
    def test_success(self) -> None:
        result = RestoreResult(
            success=True, target="/tmp/restore", snapshot_id="abc123",
            file_count=42, total_size=1024000,
        )
        assert result.success is True
        assert result.file_count == 42

    def test_failure(self) -> None:
        result = RestoreResult(
            success=False, target="/tmp/restore", snapshot_id="abc123",
            file_count=0, total_size=0,
        )
        assert result.success is False


class TestVerifyResult:
    def test_success(self) -> None:
        result = VerifyResult(
            success=True, snapshot_id="abc123", tags=["db:container"],
            file_count=100, total_size=5000,
        )
        assert result.success is True
        assert result.tags == ["db:container"]

    def test_failure(self) -> None:
        result = VerifyResult(
            success=False, snapshot_id="abc123", tags=[],
            file_count=0, total_size=0,
        )
        assert result.success is False


class TestDiffModels:
    def test_diff_entry(self) -> None:
        entry = DiffEntry(path="/data/file.txt", modifier="+")
        assert entry.path == "/data/file.txt"
        assert entry.modifier == "+"

    def test_diff_result(self) -> None:
        entries = [
            DiffEntry(path="/data/new.txt", modifier="+"),
            DiffEntry(path="/data/old.txt", modifier="-"),
        ]
        result = DiffResult(
            snapshot_id_1="aaa111", snapshot_id_2="bbb222",
            entries=entries,
        )
        assert len(result.entries) == 2
        assert result.snapshot_id_1 == "aaa111"

    def test_diff_result_empty(self) -> None:
        result = DiffResult(
            snapshot_id_1="aaa111", snapshot_id_2="bbb222",
            entries=[],
        )
        assert result.entries == []


# ── Dorestic.restore ──────────────────────────────────────


class TestDoresticRestore:
    def test_resolve_failure(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        with patch("dorestic.api.list_snapshots", return_value=[]):
            with pytest.raises(ValueError, match="No snapshot found"):
                d.restore("nonexistent")

    def test_default_target_from_tag(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        raw = [{"id": "abc123full", "short_id": "abc123", "time": "2026-07-09T02:00:00Z", "tags": ["db:container"]}]
        with patch("dorestic.api.list_snapshots", return_value=raw):
            with patch("dorestic.api.restore_snapshot", return_value=0) as mock_restore:
                result = d.restore("db:container")
        assert "restore" in result.target
        assert "db-container" in result.target
        assert mock_restore.called


# ── Dorestic.diff ────────────────────────────────────────


class TestDoresticDiff:
    def test_resolve_failure_first(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        with patch("dorestic.api.list_snapshots", return_value=[]):
            with pytest.raises(ValueError, match="No snapshot found"):
                d.diff("nonexistent", "other")

    def test_resolve_failure_second(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        raw = [{"id": "aaa111", "time": "2026-07-09T02:00:00Z", "tags": ["db"]}]
        with patch("dorestic.api.list_snapshots", return_value=raw):
            with pytest.raises(ValueError, match="No snapshot found"):
                d.diff("db", "nonexistent")

    def test_parses_diff_output(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        raw = [
            {"id": "aaa111", "short_id": "aaa111", "time": "2026-07-08T02:00:00Z", "tags": ["db"]},
            {"id": "bbb222", "short_id": "bbb222", "time": "2026-07-09T02:00:00Z", "tags": ["db2"]},
        ]
        diff_output = (
            "comparing snapshot aaa111 to bbb222\n"
            "+ /data/new.txt\n"
            "- /data/old.txt\n"
            "M /data/changed.txt\n"
            "Files:  1 new, 1 removed, 1 changed\n"
            "Added: 1.234 MiB\n"
            "Removed: 0.500 MiB"
        )
        with patch("dorestic.api.list_snapshots", return_value=raw):
            with patch("dorestic.api.diff_snapshots", return_value=(0, diff_output, "")):
                result = d.diff("aaa111", "bbb222")
        assert len(result.entries) == 3
        assert result.entries[0].modifier == "+"
        assert result.entries[0].path == "/data/new.txt"
        assert result.entries[1].modifier == "-"
        assert result.entries[2].modifier == "M"

    def test_restic_failure(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        raw = [
            {"id": "aaa111", "short_id": "aaa111", "time": "2026-07-08T02:00:00Z", "tags": ["db"]},
            {"id": "bbb222", "short_id": "bbb222", "time": "2026-07-09T02:00:00Z", "tags": ["db2"]},
        ]
        with patch("dorestic.api.list_snapshots", return_value=raw):
            with patch("dorestic.api.diff_snapshots", return_value=(1, "", "fatal error")):
                with pytest.raises(RuntimeError, match="restic diff failed"):
                    d.diff("aaa111", "bbb222")


# ── Dorestic.check ──────────────────────────────────────


class TestDoresticCheck:
    def test_returns_true_on_success(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        with patch("dorestic.api.run_restic", return_value=0):
            assert d.check() is True

    def test_returns_false_on_failure(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        with patch("dorestic.api.run_restic", return_value=1):
            assert d.check() is False


# ── Dorestic.status ─────────────────────────────────────


class TestDoresticStatus:
    def test_builds_report(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        raw_snaps = [{"id": "abc123", "time": "2026-07-09T02:00:00Z", "tags": ["db"]}]
        raw_stats = {"total_size": 5000, "total_file_count": 10}
        with patch("dorestic.api.list_snapshots", return_value=raw_snaps):
            with patch("dorestic.api.repo_stats", return_value=raw_stats):
                report = d.status()
        assert report.repository == "/repo"
        assert report.repo_stats is not None
        assert report.repo_stats.total_size == 5000
        assert len(report.snapshots) == 1

    def test_graceful_on_stats_failure(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        raw_snaps = [{"id": "abc123", "time": "2026-07-09T02:00:00Z", "tags": ["db"]}]
        with patch("dorestic.api.list_snapshots", return_value=raw_snaps):
            with patch("dorestic.api.repo_stats", side_effect=RuntimeError("fail")):
                report = d.status()
        assert report.repo_stats is None
        assert len(report.snapshots) == 1


# ── Dorestic.verify_snapshot ────────────────────────────


class TestDoresticVerifySnapshot:
    def test_specific_ref(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        raw = [{"id": "abc123full", "short_id": "abc123", "time": "2026-07-09T02:00:00Z", "tags": ["db"]}]
        with patch("dorestic.api.list_snapshots", return_value=raw):
            with patch("dorestic.api.restore_snapshot", return_value=0):
                with patch("shutil.rmtree"):
                    result = d.verify_snapshot(ref="db")
        assert result.success is True
        assert result.snapshot_id == "abc123full"
        assert result.tags == ["db"]

    def test_random_when_no_ref(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        raw = [{"id": "abc123full", "short_id": "abc123", "time": "2026-07-09T02:00:00Z", "tags": ["db"]}]
        with patch("dorestic.api.list_snapshots", return_value=raw):
            with patch("dorestic.api.restore_snapshot", return_value=0):
                with patch("shutil.rmtree"):
                    result = d.verify_snapshot()
        assert result.success is True

    def test_no_snapshots_raises(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        with patch("dorestic.api.list_snapshots", return_value=[]):
            with pytest.raises(ValueError, match="No snapshots in repository"):
                d.verify_snapshot()

    def test_restore_failure(self) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        raw = [{"id": "abc123full", "short_id": "abc123", "time": "2026-07-09T02:00:00Z", "tags": ["db"]}]
        with patch("dorestic.api.list_snapshots", return_value=raw):
            with patch("dorestic.api.restore_snapshot", return_value=1):
                with patch("shutil.rmtree"):
                    result = d.verify_snapshot(ref="db")
        assert result.success is False


# ── Dorestic.restore (additional) ───────────────────────


class TestDoresticRestoreExtra:
    def test_dry_run_does_not_create_directory(self, tmp_path: Path) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        raw = [{"id": "abc123full", "short_id": "abc123", "time": "2026-07-09T02:00:00Z", "tags": ["db"]}]
        target = str(tmp_path / "should_not_exist")
        with patch("dorestic.api.list_snapshots", return_value=raw):
            with patch("dorestic.api.restore_snapshot", return_value=0):
                result = d.restore("db", target=target, dry_run=True)
        assert result.success is True
        assert not Path(target).exists()

    def test_explicit_target(self, tmp_path: Path) -> None:
        config = BackupConfig(repository="/repo", password_file="/pw")
        d = Dorestic(config)
        raw = [{"id": "abc123full", "short_id": "abc123", "time": "2026-07-09T02:00:00Z", "tags": ["db"]}]
        target = str(tmp_path / "my_restore")
        with patch("dorestic.api.list_snapshots", return_value=raw):
            with patch("dorestic.api.restore_snapshot", return_value=0):
                result = d.restore("db", target=target)
        assert result.target == str(Path(target).resolve())


# ── print_tag_summary / print_tag_detail ────────────────


class TestPrintTagSummary:
    def _make_snap(self, tag: str, time: datetime) -> Snapshot:
        return Snapshot(
            id="a" * 64, short_id="a" * 8, tags=[tag],
            time=time, paths=["/data"], hostname="host",
        )

    def test_groups_by_tag(self, capsys: pytest.CaptureFixture[str]) -> None:
        now = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
        config = BackupConfig(repository="/r", password_file="/p")
        snaps = [
            self._make_snap("db", datetime(2026, 7, 9, 2, 0, 0, tzinfo=timezone.utc)),
            self._make_snap("db", datetime(2026, 7, 8, 2, 0, 0, tzinfo=timezone.utc)),
            self._make_snap("web", datetime(2026, 7, 9, 10, 0, 0, tzinfo=timezone.utc)),
        ]
        print_tag_summary(snaps, now, config)
        out = capsys.readouterr().out
        assert "db" in out
        assert "web" in out
        assert "2" in out  # db has 2 snapshots
        assert "1" in out  # web has 1

    def test_stale_marker(self, capsys: pytest.CaptureFixture[str]) -> None:
        now = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
        config = BackupConfig(repository="/r", password_file="/p", stale_threshold_hours=1)
        snaps = [
            self._make_snap("old", datetime(2026, 7, 8, 2, 0, 0, tzinfo=timezone.utc)),
        ]
        print_tag_summary(snaps, now, config)
        out = capsys.readouterr().out
        assert "(!)" in out


class TestPrintTagDetail:
    def test_shows_snapshots_newest_first(self, capsys: pytest.CaptureFixture[str]) -> None:
        now = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
        config = BackupConfig(repository="/r", password_file="/p")
        snaps = [
            Snapshot(
                id="older111" + "0" * 56, short_id="older111",
                tags=["db"], time=datetime(2026, 7, 8, 2, 0, 0, tzinfo=timezone.utc),
                paths=["/data"], hostname="host",
            ),
            Snapshot(
                id="newer222" + "0" * 56, short_id="newer222",
                tags=["db"], time=datetime(2026, 7, 9, 2, 0, 0, tzinfo=timezone.utc),
                paths=["/data"], hostname="host",
            ),
        ]
        print_tag_detail(snaps, now, config)
        out = capsys.readouterr().out
        newer_pos = out.index("newer222")
        older_pos = out.index("older111")
        assert newer_pos < older_pos

    def test_shows_paths(self, capsys: pytest.CaptureFixture[str]) -> None:
        now = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
        config = BackupConfig(repository="/r", password_file="/p")
        snaps = [
            Snapshot(
                id="abc12345" + "0" * 56, short_id="abc12345",
                tags=["db"], time=datetime(2026, 7, 9, 2, 0, 0, tzinfo=timezone.utc),
                paths=["/data", "/config"], hostname="host",
            ),
        ]
        print_tag_detail(snaps, now, config)
        out = capsys.readouterr().out
        assert "/data, /config" in out
