# dorestic

A label-driven backup system for Docker containers using [restic](https://restic.net/).

Containers opt in to backups by adding labels to their compose file. A host-side
Python script discovers opted-in containers, runs lifecycle hooks (e.g. database
dumps), resolves volume mounts to host paths, and spawns a disposable
`restic/restic:latest` container for each backup scope. The restic container has
no Docker socket and can only read the specific paths mounted into it.

## Installation

```bash
uv tool install dorestic
```

Or with pip:

```bash
pip install dorestic
```

## Quick Start

1. Generate an example config:

   ```bash
   dorestic init ~/.config/dorestic/
   ```

   Edit `~/.config/dorestic/config.yml` and set `repository` and `password_file`.

2. Create the password file:

   ```bash
   echo -n 'your-restic-password' > /etc/backup/restic-password
   chmod 600 /etc/backup/restic-password
   ```

   **Important:** Save this password securely (e.g. in a password manager or
   secrets vault). dorestic passes the password file to restic but does not
   store or manage the password itself. If you lose it, your restic repository
   is unrecoverable.

3. Add labels to any container you want backed up:

   ```yaml
   services:
     my-db:
       image: postgres:17
       volumes:
         - ./pgdata:/var/lib/postgresql/data
       labels:
         backup.enable: "true"
         backup.container.paths: "/var/lib/postgresql/data"
         backup.container.on_start: "pg_dumpall -U postgres --clean > /var/lib/postgresql/data/dump.sql"
         backup.container.on_complete: "rm -f /var/lib/postgresql/data/dump.sql"
         backup.host.paths: ".@1"
   ```

4. Run a backup:

   ```bash
   dorestic backup
   ```

   Or back up a single container or host group:

   ```bash
   dorestic backup --only my-db
   ```

5. Check what's been backed up:

   ```bash
   dorestic list
   ```

6. (Optional) Add to cron:

   ```
   0 3 * * * dorestic backup
   ```

## CLI Reference

```
dorestic backup                 Run a full backup (all containers + host groups)
dorestic backup --only <name>   Back up a single container or host group
dorestic backup --dry-run       Show what would be backed up without running anything
dorestic backup -v              Verbose output (debug-level: paths, restic commands)
dorestic backup -q              Quiet mode (suppress output on success, print on failure)
dorestic list                   Show snapshots grouped by tag with freshness
dorestic list --tag <tag>       Show individual snapshots for a specific tag
dorestic view <id|tag>          Show files in a snapshot (or latest for a tag)
dorestic restore <id|tag>              Restore a snapshot to a staging directory
dorestic restore <id|tag> --target DIR Restore to a specific directory
dorestic restore <id|tag> --dry-run    Preview what would be restored
dorestic verify-snapshot [ref]  Restore a snapshot to temp dir to prove recoverability
dorestic diff <snap1> <snap2>   Show what changed between two snapshots
dorestic forget-tag <tag> [...]  Permanently delete all snapshots with given tag(s)
dorestic forget-tag --untagged  Permanently delete all untagged snapshots
dorestic status                 Show repository health: size, latest backups, retention
dorestic check                  Run a repository integrity check
dorestic config-validate        Validate config and Docker labels without running a backup
dorestic init [PATH]            Write example config to PATH (default: ./)
dorestic init --refresh         Refresh existing config with latest template
```

Global flags: `--config`/`-c` to specify config path explicitly.
`-v` and `-q` are mutually exclusive.

## Configuration

Config is loaded from the first location found:

1. `./config.yml` (current directory)
2. `$XDG_CONFIG_HOME/dorestic/config.yml` (default: `~/.config/dorestic/config.yml`)

Or pass a path explicitly: `dorestic --config /path/to/config.yml backup`

Run `dorestic init [PATH]` to write an example config with documentation.

To update an existing config with the latest template (preserving your values,
adding documentation for new options):

```bash
dorestic init --refresh
```

This validates all keys (errors on anything unknown/removed), writes the new
config with your values, and saves the old file as `.bak`.

```yaml
repository: /mnt/backup/backup1
password_file: /etc/backup/restic-password

# restic_image: restic/restic:latest
# on_start: /path/to/on_start.sh
# on_complete: /path/to/on_complete.sh
# log_dir: /var/log/dorestic
# tmp_dir: /var/tmp/dorestic

# retention:
#   daily: 7
#   weekly: 4
#   monthly: 12

# host_groups:
#   - tag: documents
#     paths: [/mnt/fileserver/share]
#     exclude: ["*.tmp"]
```

The password file is mounted read-only into the restic container via
`RESTIC_PASSWORD_FILE` — the password never appears on the command line or in
`ps` output.

| Field | Required | Description |
|---|---|---|
| `repository` | Yes | Restic repository path |
| `password_file` | Yes | Path to a file containing the restic password |
| `restic_image` | No | Docker image for restic (default: `restic/restic:latest`) |
| `on_start` | No | Command to run before the backup starts. If it exits non-zero, the backup is aborted. |
| `on_complete` | No | Command to run after the entire backup. Env: `$DORESTIC_EXIT_CODE`, `$DORESTIC_LOGFILE` |
| `retention` | No | Snapshot retention policy (default: 7 daily, 4 weekly, 12 monthly) |
| `log_dir` | No | Directory for persistent backup logs. Each run writes a timestamped file. Without this, a temp log is created for `on_complete` and then deleted. |
| `tmp_dir` | No | Directory for temporary files during backup, verify, and restore (default: `/tmp`). On Linux, `/tmp` is often a RAM-backed tmpfs — if your backups are large, point this at a disk-backed path that only your user can access. The directory must already exist. |
| `stale_threshold_hours` | No | Hours after which `dorestic list` flags a tag as stale (default: 25) |
| `host_groups` | No | Host-only backup groups (see below) |

## Architecture

```
Host (dorestic)               Restic container (docker run --rm)
┌───────────────────────────────┐       ┌────────────────────────────────┐
│ Load config.yml               │       │ No Docker socket               │
│ Acquire process lock          │       │ No host filesystem access      │
│ Discover labeled containers   │       │ Only explicitly mounted paths  │
│ Run on_start hooks            │ spawn │ Data paths mounted read-only   │
│ Resolve volume mounts → paths │ ────► │ Password via mounted file      │
│ docker cp for unmounted paths │       │ Runs restic backup             │
│ Expand depth-limited paths    │       │ Deleted on completion (--rm)   │
│ Run on_complete hooks         │       └────────────────────────────────┘
│ Forget/prune/check            │
│ Run on_complete callback      │
└───────────────────────────────┘
```

The backup script runs on the host and interacts with Docker via the Python SDK.
For each backup scope, it spawns a fresh `restic/restic:latest` container with
`docker run --rm`, mounting only the necessary data paths (read-only) and the
restic repository. The container is automatically removed on completion.

## Labels

Each container can define two backup scopes — **container** (paths inside the
container, resolved via volume mounts) and **host** (paths relative to the
compose project directory).

| Label | Required | Description |
|---|---|---|
| `backup.enable` | Yes | `"true"` to opt in |
| `backup.container.paths` | No | Comma-separated container-internal paths to back up |
| `backup.container.exclude` | No | Comma-separated restic exclude patterns for container scope |
| `backup.container.on_start` | No | Command run inside the container before container backup. Env: `$DORESTIC_TAG` |
| `backup.container.on_complete` | No | Command run inside the container after container backup. Env: `$DORESTIC_TAG`, `$DORESTIC_EXIT_CODE` |
| `backup.container.shell` | No | Shell used for container hooks (default: `sh`) |
| `backup.host.paths` | No | Comma-separated paths relative to the compose project directory |
| `backup.host.exclude` | No | Comma-separated restic exclude patterns for host scope |
| `backup.host.on_start` | No | Command run on the host before host backup. Env: `$DORESTIC_TAG` |
| `backup.host.on_complete` | No | Command run on the host after host backup. Env: `$DORESTIC_TAG`, `$DORESTIC_EXIT_CODE` |
| `backup.suppress-mount-warning` | No | `"true"` to silence warnings when container paths fall back to `docker cp` (see below) |

### Container paths

Paths in `backup.container.paths` are resolved to host paths by inspecting the
container's volume mounts. The longest prefix match is used when multiple mounts
overlap.

If a path has no matching volume mount, dorestic cannot resolve it to a host
path directly. Instead, it falls back to `docker cp` to extract the data to a
temporary staging directory before backing it up. This is slower and uses extra
disk space, so dorestic logs a warning when it happens. If this is intentional
(e.g. the container stores data outside any mounted volume), set
`backup.suppress-mount-warning` to `"true"` to silence the warning.

### Host paths and depth

`backup.host.paths` are resolved relative to the container's compose project
directory (`com.docker.compose.project.working_dir`). Append `@N` to limit
depth:

| Value | Meaning |
|---|---|
| `.@1` | Top-level files in the compose dir (compose file, .env, etc.) |
| `.` | Entire compose directory, recursive |
| `../shared-config@2` | Sibling directory, 2 levels deep |

## Lifecycle

For each container, the two scopes (container and host) are independent:

```
container.on_start ─→ container backup ─→ container.on_complete
host.on_start ──────→ host backup ──────→ host.on_complete
```

- Each scope runs independently — a failing `container.on_start` does not
  affect the host scope, and vice versa.
- If `on_start` fails, the backup for that scope is skipped, but `on_complete`
  still runs with the failure code.
- `on_complete` failures log a warning but don't affect the backup's exit code.
- Hooks run inside the target container via `docker exec`, so they have access
  to that container's tools (e.g. `pg_dumpall` in a postgres image).

## Snapshots

Each container gets up to two tagged restic snapshots per run:

- `<name>:container` — container-internal paths (resolved to host via mounts)
- `<name>:host` — host/compose directory paths

### Listing snapshots

```bash
# Summary of all tags with freshness
dorestic list

# TAG                    SNAPS  LATEST               FRESHNESS
# -----------------------------------------------------------
# my-db:container            5  2026-07-09 02:00:00  9h ago
# my-db:host                 5  2026-07-09 02:00:00  9h ago
# documents                  3  2026-07-07 02:00:00  2d ago (!)
```

Tags whose latest snapshot exceeds `stale_threshold_hours` (default: 25) are
flagged with `(!)`.

```bash
# Show individual snapshots for a specific tag
dorestic list --tag my-db:container
```

### Viewing snapshot contents

```bash
# View files in a specific snapshot by ID
dorestic view abc123de

# View files in the latest snapshot for a tag
dorestic view my-db:container
```

### Restoring

Restore to a staging directory (default: `./restore/<tag>/`):

```bash
# Restore latest snapshot for a tag
dorestic restore my-db:container

# Restore a specific snapshot by ID
dorestic restore abc123de

# Restore to a specific directory
dorestic restore my-db:container --target /tmp/restore/

# Preview what would be restored
dorestic restore my-db:container --dry-run
```

Restores always go to a staging directory — never directly into running
volumes. Copy files from the staging directory to their final location after
reviewing them.

### Verifying backups

Prove that a snapshot is actually recoverable by restoring it to a temporary
directory:

```bash
# Verify a random snapshot
dorestic verify-snapshot

# Verify a specific snapshot
dorestic verify-snapshot my-db:container
```

The temp directory is automatically cleaned up after verification.

### Comparing snapshots

```bash
# Show what changed between two snapshots
dorestic diff abc123de def456ab

# Compare latest snapshots of two tags
dorestic diff my-db:container my-db:host
```

### Removing old tags

Remove all snapshots with a given tag (e.g. leftover tags from before dorestic):

```bash
# Delete all snapshots tagged 'old-backup'
dorestic forget-tag old-backup

# Delete multiple tags at once
dorestic forget-tag old-backup stale-tag host

# Delete all untagged snapshots
dorestic forget-tag --untagged

# Combine tags and --untagged
dorestic forget-tag old-backup --untagged
```

Each tag requires re-typing the name to confirm. After all tags are verified,
a single `y/N` prompt confirms the operation. The repository is pruned after
forgetting to reclaim space.

Retention policy is configurable in `config.yml`. Default: 7 daily, 4 weekly,
12 monthly. Applied per scope via `--group-by host,tags`.

## Host Backup Groups

Host-only backup groups (not tied to any container) are configured in the
`host_groups` section of `config.yml`:

```yaml
host_groups:
  - tag: documents
    paths:
      - /mnt/fileserver/share
      - /mnt/fileserver/share-private
    exclude:
      - "*.tmp"

  - tag: docker-config
    paths:
      - /mnt/fileserver/docker
    exclude:
      - "aosp-mirror/mirror"
    on_start: /config/pre-docker-backup.sh
    on_complete: /config/notify-documents.sh
```

| Field | Required | Description |
|---|---|---|
| `tag` | Yes | Restic tag for this group's snapshot |
| `paths` | Yes | List of absolute host paths to back up |
| `exclude` | No | List of restic exclude patterns |
| `on_start` | No | Command to run before backup. Env: `$DORESTIC_TAG`. Failure skips the backup. |
| `on_complete` | No | Command to run after backup. Env: `$DORESTIC_TAG`, `$DORESTIC_EXIT_CODE` |

## Examples

### Database with dump

```yaml
services:
  database:
    image: postgres:17
    volumes:
      - ${DB_DATA_LOCATION}:/var/lib/postgresql/data
    labels:
      backup.enable: "true"
      backup.container.paths: "/var/lib/postgresql/data"
      backup.container.exclude: "*.log"
      backup.container.on_start: "pg_dumpall -U postgres --clean > /var/lib/postgresql/data/dump.sql"
      backup.container.on_complete: "rm -f /var/lib/postgresql/data/dump.sql"
      backup.host.paths: ".@1"
```

The dump file is created by `on_start`, included in the restic snapshot, then
cleaned up by `on_complete` — regardless of whether the backup succeeded.

### Application with uploads

```yaml
services:
  app:
    image: myapp:latest
    volumes:
      - ${UPLOAD_LOCATION}:/usr/src/app/upload
    labels:
      backup.enable: "true"
      backup.container.paths: "/usr/src/app/upload"
      backup.container.exclude: "*.tmp,thumbs/"
      backup.host.paths: ".@1"
```

### Dry run

Use `--dry-run` to see what would be backed up without running any hooks or
restic commands. Useful when setting up labels on new containers:

```bash
dorestic backup --dry-run
```

```
my-db
  container (my-db:container)
    /srv/docker/my-db/pgdata
    exclude: *.log
    on_start: pg_dumpall -U postgres --clean > /var/lib/postgresql/data/dump.sql
    on_complete: rm -f /var/lib/postgresql/data/dump.sql
  host (my-db:host)
    /srv/docker/my-db/docker-compose.yml
    /srv/docker/my-db/.env

host:documents
  /mnt/fileserver/share
  /mnt/fileserver/share-private
  exclude: *.tmp
```

Combine with `--only` to check a single container:

```bash
dorestic backup --dry-run --only my-db
```

### Exclude pattern tips

- Patterns use restic's native `--exclude` syntax, interpreted relative to the
  backup root(s).
- Container and host scopes are separate restic invocations — `backup.host.exclude`
  never affects the container scope or other containers.
- Do **not** compress or encrypt files before handing them to restic. Restic
  already does both, and pre-processed data destroys chunk-level deduplication.

## Library Usage

dorestic can be imported and used as a library from other Python projects:

```python
from dorestic import Dorestic

# From a config file
d = Dorestic.from_config_path("/path/to/config.yml")

# Or auto-discover config (./config.yml or XDG path)
d = Dorestic.from_default_config()

# List all snapshots (returns typed Snapshot objects)
for snap in d.list_snapshots():
    print(f"{snap.short_id}  {snap.tags}  {snap.time}")

# Filter by tag
db_snaps = d.list_snapshots(tag="my-db:container")

# Resolve a snapshot by ID or tag (latest for tag)
snap = d.resolve_snapshot("my-db:container")

# View files in a snapshot (streams, constant memory)
if snap:
    for f in d.iter_snapshot_files(snap.id):
        print(f"{f.path}  ({f.size} bytes)")

# Dry run — see what would be backed up
plan = d.dry_run()
for target in plan.targets:
    print(f"{target.name}: {target.container_scope}, {target.host_scope}")

# Run a backup (acquires lock, raises RuntimeError if locked)
result = d.backup()
print(f"Success: {result.success}")

# Target a single container or host group
result = d.backup(only="my-db")

# Repository health check
report = d.status()
if report.repo_stats:
    print(f"Repo size: {report.repo_stats.total_size}")

# Integrity check
if d.check():
    print("Repository OK")

# Validate config + Docker labels
issues = d.validate()
for issue in issues:
    print(f"Warning: {issue}")

# Restore a snapshot
result = d.restore("my-db:container", target="/tmp/restore")

# Verify a random snapshot is recoverable
v = d.verify_snapshot()
print(f"Verified {v.snapshot_id[:8]}: {v.file_count} files")

# Compare two snapshots
diff = d.diff("abc123", "def456")
for entry in diff.entries:
    print(f"{entry.modifier} {entry.path}")

# Remove all snapshots with a tag (returns list of forgotten Snapshots)
forgotten = d.forget_tag("old-backup")
print(f"Forgotten {len(forgotten)} snapshots")

# Remove untagged snapshots (pass None)
d.forget_tag(None)
```

All methods return typed dataclasses (`Snapshot`, `SnapshotFile`, `BackupResult`)
instead of raw dicts or exit codes. Nothing in the library path calls `sys.exit`.

## Dependencies

- Python 3.12+
- `docker` Python SDK
- `pyyaml`
- Docker daemon with `restic/restic:latest` image available

## Testing

```bash
uv run pytest tests/ -v
```

Tests require a running Docker daemon and the `restic/restic:latest` image.
Unit tests (no Docker) can be run in isolation:

```bash
uv run pytest tests/test_unit.py -v
```

## File Structure

```
dorestic/
├── src/dorestic/
│   ├── __init__.py          # Public API re-exports
│   ├── __main__.py          # Entry point for python -m dorestic
│   ├── api.py               # Dorestic class (library interface)
│   ├── cli.py               # CLI subcommands
│   ├── display.py           # Formatting and display helpers
│   ├── models.py            # Dataclasses and constants (Snapshot, BackupResult, etc.)
│   ├── config.py            # Config file loading and validation
│   ├── config.yml.example   # Bundled example config (used by init)
│   ├── restic.py            # Restic container invocation
│   ├── docker.py            # Docker container operations
│   ├── paths.py             # Path resolution utilities
│   └── backup.py            # Backup orchestration
├── tests/
│   ├── conftest.py          # Fixtures and test infrastructure
│   ├── test_unit.py         # Pure function tests (no Docker)
│   ├── test_docker.py       # Docker integration tests
│   └── test_backup.py       # Backup execution and lifecycle tests
├── pyproject.toml
├── pyrightconfig.json
└── .gitignore
```
