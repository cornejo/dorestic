# Changelog

## v0.6.1 — 2026-09-30

### Fixed
- Two `ValueError: I/O operation on closed file.` tracebacks no longer follow every run. `TeeStream` is a `TextIOBase`, so its finalizer flushes it — at interpreter shutdown, long after the log file it wraps was closed. The tees are now closed before the log file, and flushing a closed stream is a no-op. Cosmetic only: it fired after the exit code was set and nothing was lost from the log, but Python 3.14 reports it with a full traceback where older versions were quiet

### Internal
- The Docker and restic tests now run in CI on GitHub, where a hosted runner is a VM with a real daemon. GitLab's runners are non-privileged docker executors with no socket and no dind, so they keep running `-m "not docker"` only
- The PyPI publish workflow runs the full test suite before building. A PyPI version number can never be reused, and a tag can reach that remote without the GitLab pipeline having passed
- `pypa/gh-action-pypi-publish` is pinned to a commit rather than the moving `release/v1` branch — it is the only step holding `id-token: write` against the PyPI project

## v0.6.0 — 2026-09-29

### Changed
- A backup run that previously exited 0 may now exit non-zero. A configured scope that resolves to no paths, and a failing `forget --prune` or `check`, are counted as errors rather than passed over — see Fixed below. Anything reading dorestic's exit code (a healthcheck ping, a cron wrapper) will start seeing failures it was never told about

### Fixed
- **Container paths were collected before `on_start` ran.** For a path that is not on a mount, dorestic collects it with `docker cp` — and that copy happened before the hook meant to produce the file. A `pg_dump` into `/tmp/dump.sql` was therefore copied before it existed, created by the hook, then deleted by `on_complete`, every single run. Paths are now resolved after both `on_start` hooks. Databases whose dump lands inside a mounted volume were never affected, which is why this went unnoticed
- **A scope that resolved to no paths reported success.** The failed `docker cp` above left the path list empty, which was treated as "nothing to do": exit 0, skipped, error counter untouched, healthcheck pinged green. A configured scope resolving to nothing is now a failure (exit 11, `EXIT_NO_PATHS_RESOLVED`). The same applies to a host group whose paths do not exist
- Host group paths are re-resolved after the group's `on_start`, so a hook that creates the paths works there too
- `forget --prune` and `check` exit codes are now counted as errors. A repository that failed its integrity check still finished the run green
- Snapshot timestamps carrying a UTC offset were read as if the offset were zero, shifting every non-UTC snapshot by up to 14 hours and skewing staleness reporting. The offset is now converted rather than discarded
- restic's output no longer bypasses the log file and `backup -q`. `subprocess` needs a real file descriptor, so it wrote straight to the inherited stdout instead of through `TeeStream`
- A host path spec with a depth limit (`dir@2`) expands to one entry per file, and each became its own bind mount — enough to overflow the command line on any real tree. Mounts are now collapsed to the smallest covering set of directories
- `load_config` validates config keys, the same check `config-validate` already ran. A misspelled key silently fell back to a default instead of failing the backup
- `dorestic list` and `dorestic view` crashed with `max() arg is an empty sequence` on a repository with no snapshots
- Expected failures — a missing config, an unreachable repository, a bad snapshot ref — are reported as one line on stderr instead of a traceback
- The lock file is opened with `O_NOFOLLOW` and mode 0600. Its path is derived from the repository name and `tmp_dir` defaults to a world-writable `/tmp`, so a planted symlink could redirect the truncating open
- `backup --dry-run --only <tag>` now reports the same "no match" error as the real run rather than silently planning nothing

### Internal
- The Docker availability probe actually mounts a directory and reads it back, rather than trusting `docker info`. A daemon that refuses volumes, or one that resolves paths differently than the test process, now skips the affected tests with a reason instead of producing dozens of errors

## v0.5.4 — 2026-08-07

### Fixed
- PyPI release workflow now takes the version from the tag directly rather than letting setuptools-scm shell out to `git describe`. The git-derived path could silently fall back to a placeholder version if git metadata were ever missing, as it did on the GitLab pipeline in v0.5.2 — but on PyPI a wrong version cannot be replaced, since a version number can never be reused

## v0.5.3 — 2026-08-07

v0.5.2 was tagged but never published — its pipeline built the wrong version —
so 0.5.3 is the first release carrying the v0.5.2 changes listed below.

### Fixed
- Release pipeline published `0.0.0` to the GitLab package registry instead of the tagged version. setuptools-scm shells out to the git CLI, which the build image did not have, so it fell back to a placeholder version. The published version is now taken directly from the tag

## v0.5.2 — 2026-08-07

### Fixed
- Host hooks (`host.on_start`, `host.on_complete`) now run with the compose project directory as their working directory, matching how `host.paths` specs are resolved. Previously they inherited whatever directory dorestic was launched from, so a relative path meant something different to a hook than to a path spec

### Added
- `DORESTIC_COMPOSE_DIR` is set for host hooks on container targets, so a hook can build absolute paths without hardcoding the project location. Container hooks don't receive it — it names a host path

### Changed
- Package version is now derived from the git tag via setuptools-scm instead of being hardcoded in `pyproject.toml`

### Internal
- Test suite no longer requires a `docker` binary to be present: the availability probe treats a missing binary as "unavailable" rather than raising, and the Docker-free unit tests mock out staging cleanup instead of shelling out

## v0.5.1 — 2026-07-10

### Fixed
- Fix cleanup of root-owned temp files left by Docker-based restic restores; `verify` and `backup` now use Docker to remove container-created files before host-side cleanup

## v0.5.0 — 2026-07-10

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
