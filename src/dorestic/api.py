from __future__ import annotations

from collections.abc import Generator

from dorestic.backup import acquire_lock, orchestrate_backup, plan_backup
from dorestic.config import find_config, load_config
from dorestic.models import (
    BackupConfig,
    BackupResult,
    DryRunPlan,
    Snapshot,
    SnapshotFile,
)
from dorestic.restic import iter_snapshot_files, list_snapshots


class Dorestic:
    """Library interface for dorestic backup operations.

    from dorestic import Dorestic

    d = Dorestic.from_config_path("/path/to/config.yml")
    snapshots = d.list_snapshots()
    result = d.backup(only="my-db")
    """

    def __init__(self, config: BackupConfig) -> None:
        self.config = config

    @classmethod
    def from_config_path(cls, path: str) -> Dorestic:
        return cls(load_config(path))

    @classmethod
    def from_default_config(cls) -> Dorestic:
        return cls(load_config(find_config()))

    def dry_run(self, only: str | None = None) -> DryRunPlan:
        return plan_backup(self.config, only=only)

    def backup(self, only: str | None = None) -> BackupResult:
        lock_fd = acquire_lock(self.config)
        try:
            exit_code = orchestrate_backup(self.config, only=only)
            return BackupResult(success=exit_code == 0)
        finally:
            lock_fd.close()

    def list_snapshots(self, tag: str | None = None) -> list[Snapshot]:
        raw = list_snapshots(self.config, tag=tag)
        return [Snapshot.from_restic(s) for s in raw]

    def iter_snapshot_files(
        self, snapshot_id: str,
    ) -> Generator[SnapshotFile, None, None]:
        for entry in iter_snapshot_files(self.config, snapshot_id):
            yield SnapshotFile.from_restic(entry)

    def resolve_snapshot(self, ref: str) -> Snapshot | None:
        snapshots = self.list_snapshots()
        best_tag_match: Snapshot | None = None
        for snap in snapshots:
            if snap.short_id == ref or snap.id == ref:
                return snap
            if ref in snap.tags:
                if best_tag_match is None or snap.time > best_tag_match.time:
                    best_tag_match = snap
        return best_tag_match
