from __future__ import annotations

from collections.abc import Generator

import logging
import shutil
import tempfile
from pathlib import Path

import docker

from dorestic.backup import acquire_lock, orchestrate_backup, plan_backup
from dorestic.config import find_config, load_config
from dorestic.docker import discover_targets
from dorestic.models import (
    BackupConfig,
    BackupResult,
    DiffEntry,
    DiffResult,
    DryRunPlan,
    RepoStats,
    RestoreResult,
    Snapshot,
    SnapshotFile,
    StatusReport,
    VerifyResult,
)
from dorestic.restic import (
    diff_snapshots,
    iter_snapshot_files,
    list_snapshots,
    repo_stats,
    restore_snapshot,
    run_restic,
)

DIFF_MODIFIERS = frozenset({"+", "-", "M", "T", "U"})


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

    def check(self) -> bool:
        exit_code = run_restic("check", config=self.config)
        return exit_code == 0

    def status(self) -> StatusReport:
        stats: RepoStats | None = None
        try:
            raw = repo_stats(self.config)
            stats = RepoStats(
                total_size=raw.get("total_size", 0),
                total_file_count=raw.get("total_file_count", 0),
            )
        except RuntimeError:
            logging.getLogger("backup").warning("Could not fetch repo stats")

        snapshots = self.list_snapshots()
        return StatusReport(
            repository=self.config.repository,
            retention=self.config.retention,
            repo_stats=stats,
            snapshots=snapshots,
            stale_threshold_hours=self.config.stale_threshold_hours,
            log_dir=self.config.log_dir,
        )

    def validate(self) -> list[str]:
        issues: list[str] = []
        if self.config.log_dir:
            log_dir = Path(self.config.log_dir)
            if not log_dir.exists():
                issues.append(f"log_dir does not exist: {self.config.log_dir}")
            elif not log_dir.is_dir():
                issues.append(f"log_dir is not a directory: {self.config.log_dir}")

        try:
            client = docker.DockerClient.from_env()
            targets = discover_targets(client)
            if not targets and not self.config.host_groups:
                issues.append(
                    "No backup-enabled containers found and no host_groups configured"
                )
        except Exception as e:
            issues.append(f"Cannot connect to Docker: {e}")

        return issues

    def restore(
        self,
        ref: str,
        target: str | None = None,
        dry_run: bool = False,
    ) -> RestoreResult:
        snapshot = self.resolve_snapshot(ref)
        if snapshot is None:
            raise ValueError(f"No snapshot found matching '{ref}'")

        if target is None:
            tag_part = snapshot.tags[0] if snapshot.tags else snapshot.short_id
            target = str(Path("restore") / tag_part.replace(":", "-"))

        target_path = Path(target)
        abs_target = str(target_path.resolve())

        if dry_run:
            exit_code = restore_snapshot(
                self.config, snapshot.id, abs_target, dry_run=True,
            )
            return RestoreResult(
                success=exit_code == 0,
                target=abs_target,
                snapshot_id=snapshot.id,
                file_count=0,
                total_size=0,
            )

        target_path.mkdir(parents=True, exist_ok=True)
        exit_code = restore_snapshot(
            self.config, snapshot.id, abs_target,
        )

        file_count = 0
        total_size = 0
        if exit_code == 0:
            for p in target_path.rglob("*"):
                if p.is_file():
                    file_count += 1
                    total_size += p.stat().st_size

        return RestoreResult(
            success=exit_code == 0,
            target=abs_target,
            snapshot_id=snapshot.id,
            file_count=file_count,
            total_size=total_size,
        )

    def verify_snapshot(
        self,
        ref: str | None = None,
    ) -> VerifyResult:
        if ref is not None:
            snapshot = self.resolve_snapshot(ref)
            if snapshot is None:
                raise ValueError(f"No snapshot found matching '{ref}'")
        else:
            snapshots = self.list_snapshots()
            if not snapshots:
                raise ValueError("No snapshots in repository")
            import random
            snapshot = random.choice(snapshots)

        tmp_dir = tempfile.mkdtemp(prefix="dorestic-verify-")
        try:
            exit_code = restore_snapshot(
                self.config, snapshot.id, tmp_dir,
            )
            file_count = 0
            total_size = 0
            if exit_code == 0:
                for p in Path(tmp_dir).rglob("*"):
                    if p.is_file():
                        file_count += 1
                        total_size += p.stat().st_size
            return VerifyResult(
                success=exit_code == 0,
                snapshot_id=snapshot.id,
                tags=snapshot.tags,
                file_count=file_count,
                total_size=total_size,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def diff(self, ref1: str, ref2: str) -> DiffResult:
        snapshots = self.list_snapshots()
        snap1 = self._resolve_from(ref1, snapshots)
        if snap1 is None:
            raise ValueError(f"No snapshot found matching '{ref1}'")
        snap2 = self._resolve_from(ref2, snapshots)
        if snap2 is None:
            raise ValueError(f"No snapshot found matching '{ref2}'")

        exit_code, stdout, stderr = diff_snapshots(
            self.config, snap1.id, snap2.id,
        )
        if exit_code != 0:
            raise RuntimeError(
                f"restic diff failed (exit {exit_code}): {stderr}"
            )

        entries: list[DiffEntry] = []
        for line in stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0] in DIFF_MODIFIERS:
                entries.append(DiffEntry(modifier=parts[0], path=parts[1]))
        return DiffResult(
            snapshot_id_1=snap1.id,
            snapshot_id_2=snap2.id,
            entries=entries,
        )

    @staticmethod
    def _resolve_from(
        ref: str, snapshots: list[Snapshot],
    ) -> Snapshot | None:
        best_tag_match: Snapshot | None = None
        for snap in snapshots:
            if snap.short_id == ref or snap.id == ref:
                return snap
            if ref in snap.tags:
                if best_tag_match is None or snap.time > best_tag_match.time:
                    best_tag_match = snap
        return best_tag_match

    def resolve_snapshot(self, ref: str) -> Snapshot | None:
        return self._resolve_from(ref, self.list_snapshots())
