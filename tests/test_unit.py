"""Unit tests for pure functions that don't require Docker or restic."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from dorestic import (
    DEFAULT_CONTAINER_SHELL,
    DEFAULT_STALE_THRESHOLD_HOURS,
    EXIT_ON_START_FAILED,
    BackupConfig,
    HostGroup,
    ScopeConfig,
    ScopeResult,
    TeeStream,
    acquire_lock,
    expand_depth_limited_path,
    find_config,
    load_config,
    make_restic_hostname,
    parse_comma_list,
    resolve_host_path_spec,
    run_hook,
)
from dorestic.cli import write_example_config
from dorestic.display import (
    format_freshness,
    format_size,
    is_stale,
    parse_snapshot_time,
)


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
