"""Unit tests for pure functions that don't require Docker or restic."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml

from dorestic import (
    EXIT_ON_START_FAILED,
    BackupConfig,
    HostGroup,
    ScopeConfig,
    ScopeResult,
    TeeStream,
    acquire_lock,
    auto_discover_compose_files,
    expand_depth_limited_path,
    find_config,
    load_config,
    parse_comma_list,
    resolve_host_path_spec,
)
from dorestic.cli import write_example_config


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



# ── auto_discover_compose_files ─────────────────────────────


class TestAutoDiscoverComposeFiles:
    def test_finds_files(self, tmp_path: Path) -> None:
        compose_dir = tmp_path / "project"
        compose_dir.mkdir()
        (compose_dir / "docker-compose.yml").write_text("version: '3'")
        (compose_dir / ".env").write_text("KEY=val")
        (compose_dir / "subdir").mkdir()
        (compose_dir / "subdir" / "nested.txt").write_text("x")

        files = auto_discover_compose_files(str(compose_dir))
        names = {f.name for f in files}
        assert "docker-compose.yml" in names
        assert ".env" in names
        assert "nested.txt" not in names

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        files = auto_discover_compose_files(str(tmp_path / "nope"))
        assert files == []


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

        with pytest.raises(SystemExit):
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
