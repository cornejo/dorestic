# Changelog

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
