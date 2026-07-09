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
from dorestic.paths import resolve_host_paths
from dorestic.restic import make_restic_hostname, run_restic, run_scope_backup

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
        raise RuntimeError(
            f"Another backup is already running (lock held on {lock_path})"
        )
    return lock_fd


def run_hook(command: str, env: dict[str, str] | None = None) -> int:
    hook_env = os.environ.copy()
    if env:
        hook_env.update(env)
    result = subprocess.run(["sh", "-c", command], env=hook_env)
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

    tag_env = {"DORESTIC_TAG": target.name}

    container_on_start_ok = True
    if target.container_scope and target.container_scope.on_start:
        log.info("  container.on_start: %s", target.container_scope.on_start)
        code, output = run_docker_exec(
            target.container, target.container_scope.on_start,
            env=tag_env, shell=target.container_scope.shell,
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
        code = run_hook(target.host_scope.on_start, env=tag_env)
        if code != 0:
            log.error(
                "  host.on_start failed (exit %d), skipping host backup", code
            )
            host_on_start_ok = False

    container_result = ScopeResult(exit_code=0, skipped=True)
    if container_paths and container_on_start_ok:
        container_tag = f"{target.name}:container"
        exit_code = run_scope_backup(
            container_tag,
            container_paths,
            target.container_scope.exclude if target.container_scope else [],
            config=config,
            hostname=make_restic_hostname("container", target.name),
        )
        container_result = ScopeResult(exit_code=exit_code)
        if exit_code == 0:
            log.info("  container backup OK")
        else:
            log.error("  container backup FAILED (exit %d)", exit_code)
    elif container_paths and not container_on_start_ok:
        container_result = ScopeResult(exit_code=EXIT_ON_START_FAILED, skipped=True)

    host_result = ScopeResult(exit_code=0, skipped=True)
    if host_paths and host_on_start_ok:
        host_tag = f"{target.name}:host"
        exit_code = run_scope_backup(
            host_tag,
            host_paths,
            target.host_scope.exclude if target.host_scope else [],
            config=config,
            hostname=make_restic_hostname("host", target.name),
        )
        host_result = ScopeResult(exit_code=exit_code)
        if exit_code == 0:
            log.info("  host backup OK")
        else:
            log.error("  host backup FAILED (exit %d)", exit_code)
    elif host_paths and not host_on_start_ok:
        host_result = ScopeResult(exit_code=EXIT_ON_START_FAILED, skipped=True)

    if target.container_scope and target.container_scope.on_complete:
        log.info("  container.on_complete: %s", target.container_scope.on_complete)
        code, output = run_docker_exec(
            target.container, target.container_scope.on_complete,
            env={**tag_env, "DORESTIC_EXIT_CODE": str(container_result.exit_code)},
            shell=target.container_scope.shell,
        )
        if output:
            for line in output.splitlines():
                log.info("    %s", line)
        if code != 0:
            log.warning("  container.on_complete failed (exit %d)", code)

    if target.host_scope and target.host_scope.on_complete:
        log.info("  host.on_complete: %s", target.host_scope.on_complete)
        code = run_hook(
            target.host_scope.on_complete,
            env={**tag_env, "DORESTIC_EXIT_CODE": str(host_result.exit_code)},
        )
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

    tag_env = {"DORESTIC_TAG": group.tag}

    on_start_ok = True
    if group.on_start:
        log.info("  on_start: %s", group.on_start)
        code = run_hook(group.on_start, env=tag_env)
        if code != 0:
            log.error("  on_start failed (exit %d), skipping backup", code)
            on_start_ok = False

    if on_start_ok:
        exit_code = run_scope_backup(
            group.tag, resolved_paths, group.exclude, config=config,
            hostname=make_restic_hostname("host", group.tag),
        )
        result = ScopeResult(exit_code=exit_code)
        if exit_code == 0:
            log.info("  backup OK")
        else:
            log.error("  backup FAILED (exit %d)", exit_code)
    else:
        result = ScopeResult(exit_code=EXIT_ON_START_FAILED, skipped=True)

    if group.on_complete:
        log.info("  on_complete: %s", group.on_complete)
        code = run_hook(group.on_complete, env={**tag_env, "DORESTIC_EXIT_CODE": str(result.exit_code)})
        if code != 0:
            log.warning("  on_complete failed (exit %d)", code)

    return result


def _init_repo(config: BackupConfig) -> None:
    log.info("=== Initializing repository if needed ===")
    init_code, init_stdout, init_stderr = run_restic("init", config=config, capture=True)
    if init_code == 0:
        log.info("Initialized new repository at %s", config.repository)
        return
    check_code, _, _ = run_restic("cat", "config", config=config, capture=True)
    if check_code != 0:
        init_output = (init_stdout + "\n" + init_stderr).strip()
        raise RuntimeError(
            f"Repository init failed at {config.repository}:\n{init_output}"
        )


def orchestrate_backup(
    config: BackupConfig,
    only: str | None = None,
    log_path: str | None = None,
) -> int:
    targeted = only is not None

    if not targeted and config.on_start:
        log.info("Running on_start: %s", config.on_start)
        start_code = run_hook(config.on_start)
        if start_code != 0:
            log.error("on_start failed (exit %d), aborting backup", start_code)
            return 1

    _init_repo(config)

    client = docker.DockerClient.from_env()
    errors = 0
    staging_dir = Path(tempfile.mkdtemp(prefix="backup-staging-"))

    try:
        log.info("")
        log.info("=== Discovering backup-enabled containers ===")
        targets = discover_targets(client)

        if targeted:
            targets = [t for t in targets if t.name == only]
            if not targets and not any(g.tag == only for g in config.host_groups):
                log.error("No container or host group found matching '%s'", only)
                return 1

        if not targets:
            if not targeted:
                log.info("  No backup-enabled containers found")

        for target in targets:
            container_result, host_result = backup_container(
                target, config=config, staging_dir=staging_dir,
            )
            if container_result.exit_code != 0:
                errors += 1
            if host_result.exit_code != 0:
                errors += 1

        host_groups = config.host_groups
        if targeted:
            host_groups = [g for g in host_groups if g.tag == only]

        if host_groups:
            log.info("")
            log.info("=== Processing host backup groups ===")
            for group in host_groups:
                group_result = backup_host_group(group, config=config)
                if group_result.exit_code != 0:
                    errors += 1

        if not targeted:
            log.info("")
            log.info("=== Forgetting old snapshots and pruning ===")
            run_restic(
                "forget",
                "--group-by", "host,tags",
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

        if not targeted and config.on_complete:
            log.info("Running on_complete: %s", config.on_complete)
            run_hook(config.on_complete, env={
                "DORESTIC_EXIT_CODE": str(overall_exit),
                **({"DORESTIC_LOGFILE": log_path} if log_path else {}),
            })

        return overall_exit
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def run_backup(config_path: str, only: str | None = None) -> None:
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
        log_file.flush()
        overall_exit = orchestrate_backup(config, only=only, log_path=log_path)
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
        if lock_fd is not None:
            lock_fd.close()

    sys.exit(overall_exit)
