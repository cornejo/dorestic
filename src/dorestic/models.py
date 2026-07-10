from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from docker.models.containers import Container

DEFAULT_LABEL_PREFIX = "backup"
DEFAULT_RESTIC_IMAGE = "restic/restic:latest"
EXIT_ON_START_FAILED = 10


def parse_snapshot_time(time_str: str) -> datetime:
    cleaned = time_str.rstrip("Z")
    if "." in cleaned:
        base, frac = cleaned.rsplit(".", 1)
        frac = frac[:6]
        cleaned = f"{base}.{frac}"
        return datetime.fromisoformat(cleaned).replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(cleaned).replace(tzinfo=timezone.utc)


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
