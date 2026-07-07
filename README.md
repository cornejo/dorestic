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

Or directly from a git repository:

```bash
uv tool install git+https://github.com/USER/dorestic.git
```

## Quick Start

1. Generate an example config:

   ```bash
   dorestic --init ~/.config/dorestic/
   ```

   Edit `~/.config/dorestic/config.yml` and set `repository` and `password_file`.

2. Create the password file:

   ```bash
   echo -n 'your-restic-password' > /etc/backup/restic-password
   chmod 600 /etc/backup/restic-password
   ```

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
   dorestic
   ```

5. (Optional) Add to cron:

   ```
   0 3 * * * dorestic
   ```

## Configuration

Config is loaded from the first location found:

1. `./config.yml` (current directory)
2. `$XDG_CONFIG_HOME/dorestic/config.yml` (default: `~/.config/dorestic/config.yml`)

Or pass a path explicitly: `dorestic /path/to/config.yml`

Run `dorestic --init [PATH]` to write an example config with documentation.

```yaml
repository: /mnt/backup/backup1
password_file: /etc/backup/restic-password

# restic_image: restic/restic:latest
# on_start: /path/to/on_start.sh
# on_complete: /path/to/on_complete.sh

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
| `on_start` | No | Script to run before the backup starts. If it exits non-zero, the backup is aborted. |
| `on_complete` | No | Script to run after the entire backup (`--exit-code N --logfile PATH`) |
| `retention` | No | Snapshot retention policy (default: 7 daily, 4 weekly, 12 monthly) |
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
| `backup.container.on_start` | No | Shell command run inside the container before container backup (`--tag NAME`) |
| `backup.container.on_complete` | No | Shell command run inside the container after container backup (`--exit-code N --tag NAME`) |
| `backup.host.paths` | No | Comma-separated paths relative to the compose project directory |
| `backup.host.exclude` | No | Comma-separated restic exclude patterns for host scope |
| `backup.host.on_start` | No | Shell command run inside the container before host backup (`--tag NAME`) |
| `backup.host.on_complete` | No | Shell command run inside the container after host backup (`--exit-code N --tag NAME`) |
| `backup.suppress-mount-warning` | No | `"true"` to silence warnings about unmounted paths |

### Container paths

Paths in `backup.container.paths` are resolved to host paths by inspecting the
container's volume mounts. The longest prefix match is used when multiple mounts
overlap. If a path has no matching mount, it is extracted via `docker cp` to a
staging directory (with a warning unless suppressed).

### Host paths and depth

`backup.host.paths` are resolved relative to the container's compose project
directory (`com.docker.compose.project.working_dir`). Append `@N` to limit
depth:

| Value | Meaning |
|---|---|
| `.@1` | Top-level files in the compose dir (compose file, .env, etc.) |
| `.` | Entire compose directory, recursive |
| `../shared-config@2` | Sibling directory, 2 levels deep |

### Auto-discovered compose config

Every opted-in container managed by Docker Compose automatically gets its compose
project directory's top-level files included in the host-scope backup. This is
equivalent to an implicit `.@1` — `docker-compose.yml`, `.env`, `Dockerfile`,
and other config files are always backed up. These are deduplicated with any
explicitly declared `backup.host.paths`.

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

```bash
# List all snapshots
restic snapshots

# List snapshots for a specific container scope
restic snapshots --tag my-db:container

# List host-scope snapshots
restic snapshots --tag my-db:host

# Restore a specific scope
restic restore --tag my-db:container latest --target /tmp/restore/
```

Retention policy is configurable in `config.yml`. Default: 7 daily, 4 weekly,
12 monthly. Applied per tag via `--group-by tags`.

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
| `on_start` | No | Script to run before backup (`--tag TAG`). Failure skips the backup. |
| `on_complete` | No | Script to run after backup (`--exit-code N --tag TAG`) |

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

### Exclude pattern tips

- Patterns use restic's native `--exclude` syntax, interpreted relative to the
  backup root(s).
- Container and host scopes are separate restic invocations — `backup.host.exclude`
  never affects the container scope or other containers.
- Do **not** compress or encrypt files before handing them to restic. Restic
  already does both, and pre-processed data destroys chunk-level deduplication.

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
│   ├── cli.py               # Argument parsing and --init
│   ├── models.py            # Dataclasses and constants
│   ├── config.py            # Config file loading and validation
│   ├── config.yml.example   # Bundled example config (used by --init)
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
