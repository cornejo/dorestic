# Changelog

## v0.5.0 — 2026-07-09

### Added
- `Dorestic` class — first-class library interface for importing dorestic from other Python projects
- Typed models: `Snapshot`, `SnapshotFile`, `BackupResult` replace raw dicts and exit codes
- `dorestic init --refresh` — refresh existing config with latest template, validating all keys and preserving values (old config saved as `.bak`)
- `dorestic list` — show snapshots grouped by tag with freshness and staleness markers
- `dorestic list --tag <tag>` — show individual snapshots for a specific tag
- `dorestic view <snapshot|tag>` — show files in a snapshot or latest for a tag
- `dorestic backup --only <name>` — back up a single container or host group (skips global hooks, prune, and check)
- `dorestic restore <id|tag>` — restore a snapshot to a staging directory (default `./restore/<tag>/`), with `--target` and `--dry-run`
- `dorestic verify-snapshot [ref]` — restore a snapshot to a temp dir to prove recoverability (random snapshot if no ref given)
- `dorestic diff <snap1> <snap2>` — show what changed between two snapshots (wraps `restic diff`, resolves tags to latest)
- `dorestic forget-tag <tag> [...]` — permanently delete all snapshots with given tag(s), with per-tag name confirmation and a single final `y/N` prompt before acting
- `dorestic forget-tag --untagged` — permanently delete all untagged snapshots (can be combined with named tags)
- `dorestic status` — dashboard showing repository size, latest backup per scope, retention policy, and staleness
- `dorestic check` — standalone repository integrity check (previously only ran as part of a full backup)
- `dorestic config-validate` — validate config file and Docker container labels without running a backup
- `dorestic backup --dry-run` — show what would be backed up without running hooks or restic
- `dorestic backup -v` — verbose/debug output (resolved paths, mount mappings, restic commands)
- `dorestic backup -q` — quiet mode (suppress output on success, print everything on failure)
- `log_dir` config option — directory for persistent timestamped backup logs; without it, a temp log is created for `on_complete` and then deleted
- `tmp_dir` config option — directory for temporary files during backup, verify, and restore (default: `/tmp`); use a disk-backed path for large backups since `/tmp` is often a RAM-backed tmpfs on Linux
- `stale_threshold_hours` config option (default: 25) for controlling staleness markers in `list` output
- `--config` / `-c` top-level flag to specify config path explicitly
- New `api.py` and `display.py` modules

### Changed
- `dorestic` with no args (or `-h`) now shows a clean grouped command listing instead of the default argparse error
- CLI commands (`list`, `view`) now use `Dorestic` class internally — CLI is a thin layer over the library
- `_resolve_snapshot` now uses O(n) single-pass algorithm instead of O(n^2)
- `acquire_lock` raises `RuntimeError` instead of calling `sys.exit(1)` — nothing in the library path calls `sys.exit`
- `backup_host_group` uses flag-based flow instead of early return, eliminating duplicated `on_complete` hook call
- `iter_snapshot_files` uses proper `if` guards instead of `assert` for stdout/stderr checks
- `restic snapshots --json` output parsed with stdout/stderr kept separate to prevent JSON corruption from warnings
- `iter_snapshot_files` streams JSONL via `subprocess.Popen` for constant memory usage
- `parse_snapshot_time` moved from `display` to `models` (co-located with `Snapshot.from_restic`)
- Fixed stale help text in `config.py` (`dorestic --init` → `dorestic init`)

## v0.4.4 — 2026-07-09

### Changed
- PyPI publish workflow now triggers on tag push (`v*`) instead of GitHub release

## v0.4.3 — 2026-07-09

### Added
- GitHub Actions CI workflow running unit tests on Python 3.12 and 3.13

### Fixed
- Remove license classifier conflicting with PEP 639 `license` field
- Use `[dependency-groups]` instead of `[project.optional-dependencies]` for dev deps

## v0.4.1 — 2026-07-09

### Added
- PyPI project metadata (description, license, authors, classifiers, keywords, URLs)
- Apache 2.0 LICENSE file
- GitHub Actions workflow for automated PyPI publishing via trusted publishers
- GitHub FUNDING.yml for Ko-fi and Buy Me a Coffee sponsor links

## v0.4.0 — 2026-07-07

### Added
- Deterministic hostname per backup scope for restic incremental scan optimization
- 26 new tests covering hooks, env vars, shell config, hostname passthrough, and scope logging

### Changed
- `forget` now groups by `host,tags` to correctly apply retention per scope

## v0.3.0 — 2026-07-07

### Changed
- **Breaking**: hooks now use `DORESTIC_TAG`, `DORESTIC_EXIT_CODE`, and `DORESTIC_LOGFILE` env vars instead of named flags
- **Breaking**: host scope `on_start`/`on_complete` now run on the host, not inside the container
- **Breaking**: `backup.enable=true` without `container.paths` or `host.paths` is now a hard error
- All hooks run via `sh -c` (both host and container)

### Added
- `backup.container.shell` label to configure the shell for container hooks (default: `sh`)
- Documentation for `suppress-mount-warning` label and `docker cp` fallback

### Removed
- Auto-discovery of compose files (undocumented implicit behavior)

## v0.2.1 — 2026-07-07

### Fixed
- Suppress alarming "Fatal: config file already exists" from restic init when repository already exists
- Add OK/FAILED log lines after each scope backup so the log file records outcomes

## v0.2.0 — 2026-07-07

### Added
- `on_start` hook for top-level config — runs before the backup begins, aborts on failure
- `--tag` argument passed to container `on_start` and `on_complete` hooks

### Changed
- **Breaking**: all hook scripts now receive named flags (`--exit-code`, `--tag`, `--logfile`) instead of positional arguments
- Container hooks pass args as shell positional params via `sh -c` instead of string concatenation
- Password file documentation updated to note trailing newlines are fine

## v0.1.1 — 2026-07-07

### Fixed
- `--init` with a non-existent directory path created a file instead of a directory with `config.yml` inside it

## v0.1.0 — 2026-07-06

### Added
- Proper Python package (`src/dorestic/`) installable via `uv tool install`
- CLI with `--init` to write bundled example config to any path
- XDG-compliant config search (`./config.yml` → `~/.config/dorestic/config.yml`)
- Config validation: required fields, `password_file` existence, `excludes` typo detection
- Docker label `excludes` typo detection with clear error message
- Per-repository lock file (different repos can run in parallel)
- `restic init` error detection (distinguishes "already initialized" from real failures)
- `try/finally` cleanup in `run_backup` (streams, lock, temp files always restored)
- 102 tests with pyright strict (0 errors)

### Changed
- Renamed project from `restic-backup` to `dorestic`
- Split monolithic `config/backup.py` into 7 focused modules
- Password always via `RESTIC_PASSWORD_FILE` mount (never on command line)
- Log file created with 0600 permissions and cleaned up after `on_complete`
- Lock file derived from repository path hash instead of global `/tmp/backup.lock`

### Removed
- `do_backup.sh`, `requirements.txt`, `plan.md` (stale v1 artifacts)
- `Dockerfile` and `docker-compose.yml` (no longer needed)
- `.env.example` (replaced by `dorestic --init`)
