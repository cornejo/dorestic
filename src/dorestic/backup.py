from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from io import TextIOBase
from pathlib import Path
from typing import IO

import docker

from dorestic.config import load_config
from dorestic.docker import (
    discover_targets,
    resolve_container_paths,
    run_docker_exec,
)
from dorestic.models import (
    BackupConfig,
    ContainerTarget,
    EXIT_ON_START_FAILED,
    HostGroup,
    ScopeResult,
)
from dorestic.paths import get_auto_discovered_paths, resolve_host_paths
from dorestic.restic import run_restic, run_scope_backup

log = logging.getLogger("backup")


class TeeStream(TextIOBase):
    """Writes to both a file and the original stream."""

    def __init__(self, original: IO[str], log_file: IO[str]) -> None:
        self.original = original
        self.log_file = log_file

    def write(self, s: str) -> int:
        self.original.write(s)
        self.log_file.write(s)
        return len(s)

    def flush(self) -> None:
        self.original.flush()
        self.log_file.flush()


def _lock_path_for(config: BackupConfig) -> Path:
    """Derive a per-repository lock file path."""
    repo_hash = hashlib.sha256(config.repository.encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"dorestic-{repo_hash}.lock"


def acquire_lock(config: BackupConfig) -> IO[str]:
    lock_path = _lock_path_for(config)
    lock_fd: IO[str] = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("Another backup is already running (lock held on %s)", lock_path)
        sys.exit(1)
    return lock_fd


def run_host_script(script: str, *args: str) -> int:
    result = subprocess.run([script, *args])
    return result.returncode


def backup_container(
    target: ContainerTarget,
    config: BackupConfig,
    staging_dir: Path | None = None,
) -> tuple[ScopeResult, ScopeResult]:
    log.info("")
    log.info("=== %s ===", target.name)

    container_paths = resolve_container_paths(target, staging_dir=staging_dir)
    host_paths = resolve_host_paths(target)

    auto_paths = get_auto_discovered_paths(target)
    if auto_paths:
        existing_set = set(host_paths)
        for p in auto_paths:
            if p not in existing_set:
                host_paths.append(p)

    container_on_start_ok = True
    if target.container_scope and target.container_scope.on_start:
        log.info("  container.on_start: %s", target.container_scope.on_start)
        code, output = run_docker_exec(
            target.container, target.container_scope.on_start,
            "--tag", target.name,
        )
        if output:
            for line in output.splitlines():
                log.info("    %s", line)
        if code != 0:
            log.error(
                "  container.on_start failed (exit %d), skipping container backup",
                code,
            )
            container_on_start_ok = False

    host_on_start_ok = True
    if target.host_scope and target.host_scope.on_start:
        log.info("  host.on_start: %s", target.host_scope.on_start)
        code, output = run_docker_exec(
            target.container, target.host_scope.on_start,
            "--tag", target.name,
        )
        if output:
            for line in output.splitlines():
                log.info("    %s", line)
        if code != 0:
            log.error(
                "  host.on_start failed (exit %d), skipping host backup", code
            )
            host_on_start_ok = False

    container_result = ScopeResult(exit_code=0, skipped=True)
    if container_paths and container_on_start_ok:
        container_result = ScopeResult(
            exit_code=run_scope_backup(
                f"{target.name}:container",
                container_paths,
                target.container_scope.exclude if target.container_scope else [],
                config=config,
            )
        )
    elif container_paths and not container_on_start_ok:
        container_result = ScopeResult(exit_code=EXIT_ON_START_FAILED, skipped=True)

    host_result = ScopeResult(exit_code=0, skipped=True)
    if host_paths and host_on_start_ok:
        host_result = ScopeResult(
            exit_code=run_scope_backup(
                f"{target.name}:host",
                host_paths,
                target.host_scope.exclude if target.host_scope else [],
                config=config,
            )
        )
    elif host_paths and not host_on_start_ok:
        host_result = ScopeResult(exit_code=EXIT_ON_START_FAILED, skipped=True)

    if target.container_scope and target.container_scope.on_complete:
        log.info("  container.on_complete: %s", target.container_scope.on_complete)
        code, output = run_docker_exec(
            target.container, target.container_scope.on_complete,
            "--exit-code", str(container_result.exit_code), "--tag", target.name,
        )
        if output:
            for line in output.splitlines():
                log.info("    %s", line)
        if code != 0:
            log.warning("  container.on_complete failed (exit %d)", code)

    if target.host_scope and target.host_scope.on_complete:
        log.info("  host.on_complete: %s", target.host_scope.on_complete)
        code, output = run_docker_exec(
            target.container, target.host_scope.on_complete,
            "--exit-code", str(host_result.exit_code), "--tag", target.name,
        )
        if output:
            for line in output.splitlines():
                log.info("    %s", line)
        if code != 0:
            log.warning("  host.on_complete failed (exit %d)", code)

    return container_result, host_result


def backup_host_group(group: HostGroup, config: BackupConfig) -> ScopeResult:
    log.info("")
    log.info("=== host:%s ===", group.tag)

    resolved_paths: list[Path] = []
    for raw in group.paths:
        full = Path(raw)
        if full.exists():
            resolved_paths.append(full)
        else:
            log.warning("  host path %s does not exist", full)

    if not resolved_paths:
        log.info("  no valid paths, skipping")
        return ScopeResult(exit_code=0, skipped=True)

    if group.on_start:
        log.info("  on_start: %s", group.on_start)
        code = run_host_script(group.on_start, "--tag", group.tag)
        if code != 0:
            log.error("  on_start failed (exit %d), skipping backup", code)
            result = ScopeResult(exit_code=EXIT_ON_START_FAILED, skipped=True)
            if group.on_complete:
                log.info("  on_complete: %s", group.on_complete)
                run_host_script(group.on_complete, "--exit-code", str(result.exit_code), "--tag", group.tag)
            return result

    exit_code = run_scope_backup(group.tag, resolved_paths, group.exclude, config=config)
    result = ScopeResult(exit_code=exit_code)

    if group.on_complete:
        log.info("  on_complete: %s", group.on_complete)
        code = run_host_script(group.on_complete, "--exit-code", str(result.exit_code), "--tag", group.tag)
        if code != 0:
            log.warning("  on_complete failed (exit %d)", code)

    return result


def run_backup(config_path: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_file = tempfile.NamedTemporaryFile(
        mode="w", prefix="backup-", suffix=".log", delete=False
    )
    log_path = log_file.name
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    overall_exit = 1
    lock_fd: IO[str] | None = None
    staging_dir: Path | None = None

    tee_stdout = TeeStream(original_stdout, log_file)
    tee_stderr = TeeStream(original_stderr, log_file)

    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.stream = tee_stdout

    sys.stdout = tee_stdout  # type: ignore[assignment]
    sys.stderr = tee_stderr  # type: ignore[assignment]

    try:
        config = load_config(config_path)
        lock_fd = acquire_lock(config)

        if config.on_start:
            log.info("Running on_start: %s", config.on_start)
            start_code = run_host_script(config.on_start)
            if start_code != 0:
                log.error("on_start failed (exit %d), aborting backup", start_code)
                sys.exit(1)

        log.info("=== Initializing repository if needed ===")
        init_code = run_restic("init", config=config)
        if init_code != 0:
            if run_restic("cat", "config", config=config) != 0:
                log.error("Repository init failed and no existing repo found at %s", config.repository)
                sys.exit(1)

        client = docker.DockerClient.from_env()
        errors = 0
        staging_dir = Path(tempfile.mkdtemp(prefix="backup-staging-"))

        log.info("")
        log.info("=== Discovering backup-enabled containers ===")
        targets = discover_targets(client)

        if not targets:
            log.info("  No backup-enabled containers found")

        for target in targets:
            container_result, host_result = backup_container(
                target, config=config, staging_dir=staging_dir,
            )
            if container_result.exit_code != 0:
                errors += 1
            if host_result.exit_code != 0:
                errors += 1

        if config.host_groups:
            log.info("")
            log.info("=== Processing host backup groups ===")
            for group in config.host_groups:
                group_result = backup_host_group(group, config=config)
                if group_result.exit_code != 0:
                    errors += 1

        log.info("")
        log.info("=== Forgetting old snapshots and pruning ===")
        run_restic(
            "forget",
            "--group-by", "tags",
            "--keep-daily", str(config.retention.daily),
            "--keep-weekly", str(config.retention.weekly),
            "--keep-monthly", str(config.retention.monthly),
            "--prune",
            config=config,
        )

        log.info("")
        log.info("=== Checking repository integrity ===")
        run_restic("check", config=config)

        overall_exit = 1 if errors > 0 else 0

        log.info("")
        log.info("=== Backup complete ===")
        if errors > 0:
            log.warning("%d backup(s) had errors", errors)

        if config.on_complete:
            log_file.flush()
            log.info("Running on_complete: %s", config.on_complete)
            run_host_script(config.on_complete, "--exit-code", str(overall_exit), "--logfile", log_path)

    except SystemExit:
        raise
    except Exception:
        log.exception("Backup failed with unexpected error")
        overall_exit = 1
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        for handler in root_logger.handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.stream = original_stderr
        if not log_file.closed:
            log_file.close()
        os.unlink(log_path)
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
        if lock_fd is not None:
            lock_fd.close()

    sys.exit(overall_exit)


