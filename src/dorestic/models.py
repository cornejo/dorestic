from __future__ import annotations

from dataclasses import dataclass, field

from docker.models.containers import Container

DEFAULT_LABEL_PREFIX = "backup"
DEFAULT_RESTIC_IMAGE = "restic/restic:latest"
EXIT_ON_START_FAILED = 10


@dataclass
class ScopeResult:
    exit_code: int
    skipped: bool = False


@dataclass
class ScopeConfig:
    paths: list[str]
    exclude: list[str] = field(default_factory=lambda: list[str]())
    on_start: str | None = None
    on_complete: str | None = None


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
class BackupConfig:
    repository: str
    password_file: str
    restic_image: str = DEFAULT_RESTIC_IMAGE
    on_start: str | None = None
    on_complete: str | None = None
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    host_groups: list[HostGroup] = field(default_factory=lambda: list[HostGroup]())
