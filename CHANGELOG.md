# Changelog

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
