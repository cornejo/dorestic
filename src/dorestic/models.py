from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from docker.models.containers import Container

DEFAULT_LABEL_PREFIX = "backup"
CONTAINER_MOUNT_ROOT = "/dorestic"
DEFAULT_RESTIC_IMAGE = "restic/restic:latest"
# Deliberately above restic's own range. restic exits 10 if the repository does
# not exist, 11 if it is already locked, and 12 if the password is incorrect.
# A scope's exit code is either one of these constants or restic's code passed
# through the same channel, so an overlap makes "no paths resolved" and
# "repository is locked" indistinguishable to anything reading the code.
EXIT_ON_START_FAILED = 64
EXIT_NO_PATHS_RESOLVED = 65
EXIT_UNMOUNTABLE_PATH = 66

# Trims a sub-second fraction to the six digits fromisoformat accepts, leaving
# any trailing UTC offset (or "Z") in group 2 untouched.
_NANOS = re.compile(r"^(.*?\.\d{1,6})\d*(.*)$")


def parse_snapshot_time(time_str: str) -> datetime:
    """Parse a restic snapshot timestamp into an aware UTC datetime.

    Restic emits RFC 3339 with the offset of whichever machine wrote the
    snapshot and nanosecond precision, so the fraction is truncated and the
    offset is *converted* rather than overwritten. A timestamp carrying no
    offset at all is assumed to be UTC.
    """
    m = _NANOS.match(time_str)
    cleaned = m.group(1) + m.group(2) if m else time_str
    parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class BackupPath:
    """One path handed to restic, tagged with the namespace `source` lives in.

    `source` is what the daemon bind-mounts; `target` is where that appears
    inside the restic container and is therefore what restic records as the
    snapshot path.

    `daemon_sourced` marks a source that came from `docker inspect`
    Mounts[].Source. Such a path is meaningful only to the daemon — under
    Docker Desktop, rootless userns, or a remote DOCKER_HOST it may name
    nothing at all in our own mount namespace — so it must never be stat'd,
    collapsed against local paths, or rewritten. It is only ever handed back
    to the daemon, which is where it came from.
    """

    source: Path
    target: Path
    daemon_sourced: bool = False

    @property
    def remapped(self) -> bool:
        """True when `target` differs from `source`, pinning the snapshot path."""
        return self.source != self.target


@dataclass
class Snapshot:
    id: str
    short_id: str
    time: datetime
    tags: list[str]
    paths: list[str]
    hostname: str

    @classmethod
    def from_restic(cls, data: dict[str, Any]) -> Snapshot:
        return cls(
            id=data["id"],
            short_id=data.get("short_id", data["id"][:8]),
            time=parse_snapshot_time(data["time"]),
            tags=data.get("tags") or [],
            paths=data.get("paths", []),
            hostname=data.get("hostname", ""),
        )


@dataclass
class SnapshotFile:
    path: str
    type: str
    size: int

    @classmethod
    def from_restic(cls, data: dict[str, Any]) -> SnapshotFile:
        return cls(
            path=data.get("path", ""),
            type=data.get("type", ""),
            size=data.get("size", 0),
        )


@dataclass
class BackupResult:
    success: bool


@dataclass
class ScopeResult:
    exit_code: int
    skipped: bool = False


DEFAULT_CONTAINER_SHELL = "sh"


@dataclass
class ScopeConfig:
    paths: list[str]
    exclude: list[str] = field(default_factory=lambda: list[str]())
    on_start: str | None = None
    on_complete: str | None = None
    shell: str = DEFAULT_CONTAINER_SHELL


@dataclass
class ContainerTarget:
    name: str
    container: Container
    container_scope: ScopeConfig | None = None
    host_scope: ScopeConfig | None = None
    suppress_mount_warning: bool = False
    compose_dir: str | None = None


@dataclass
class HostGroup:
    tag: str
    paths: list[str]
    exclude: list[str] = field(default_factory=lambda: list[str]())
    on_start: str | None = None
    on_complete: str | None = None


@dataclass
class RetentionPolicy:
    daily: int = 7
    weekly: int = 4
    monthly: int = 12


@dataclass
class DryRunScope:
    tag: str
    paths: list[str]
    exclude: list[str]
    on_start: str | None = None
    on_complete: str | None = None


@dataclass
class DryRunTarget:
    name: str
    container_scope: DryRunScope | None = None
    host_scope: DryRunScope | None = None


@dataclass
class DryRunPlan:
    targets: list[DryRunTarget]
    host_groups: list[DryRunScope]
    global_on_start: str | None = None
    global_on_complete: str | None = None


@dataclass
class RestoreResult:
    success: bool
    target: str
    snapshot_id: str
    file_count: int
    total_size: int


@dataclass
class VerifyResult:
    success: bool
    snapshot_id: str
    tags: list[str]
    file_count: int
    total_size: int


@dataclass
class DiffEntry:
    path: str
    modifier: str


@dataclass
class DiffResult:
    snapshot_id_1: str
    snapshot_id_2: str
    entries: list[DiffEntry]


@dataclass
class RepoStats:
    total_size: int
    total_file_count: int


@dataclass
class StatusReport:
    repository: str
    retention: RetentionPolicy
    repo_stats: RepoStats | None
    snapshots: list[Snapshot]
    stale_threshold_hours: int
    log_dir: str | None


DEFAULT_STALE_THRESHOLD_HOURS = 25


@dataclass
class BackupConfig:
    repository: str
    password_file: str
    restic_image: str = DEFAULT_RESTIC_IMAGE
    on_start: str | None = None
    on_complete: str | None = None
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    host_groups: list[HostGroup] = field(default_factory=lambda: list[HostGroup]())
    stale_threshold_hours: int = DEFAULT_STALE_THRESHOLD_HOURS
    log_dir: str | None = None
    tmp_dir: str = "/tmp"
