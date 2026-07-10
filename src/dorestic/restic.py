from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import Any, Literal, overload

from dorestic.models import BackupConfig

log = logging.getLogger("backup")

MAX_HOSTNAME_LEN = 63


def make_restic_hostname(scope: str, tag: str) -> str:
    """Build a deterministic hostname for restic parent-snapshot matching.

    Docker hostnames follow RFC 1123: alphanumeric and hyphens, max 63 chars.
    """
    base = re.sub(r"[^a-zA-Z0-9-]", "-", f"dorestic-{scope}-{tag}")
    if len(base) <= MAX_HOSTNAME_LEN:
        return base
    prefix = base[: MAX_HOSTNAME_LEN - 9]
    suffix = hashlib.sha256(base.encode()).hexdigest()[:8]
    return f"{prefix}-{suffix}"


def _build_restic_cmd(config: BackupConfig) -> list[str]:
    password_mount = "/run/secrets/restic-password"
    cmd: list[str] = [
        "docker", "run", "--rm",
        "-e", f"RESTIC_REPOSITORY={config.repository}",
        "-e", f"RESTIC_PASSWORD_FILE={password_mount}",
        "-v", f"{config.password_file}:{password_mount}:ro",
    ]
    if Path(config.repository).is_absolute():
        cmd.extend(["-v", f"{config.repository}:{config.repository}"])
    return cmd


@overload
def run_restic(
    *args: str,
    config: BackupConfig,
    mount_paths: list[Path] | None = None,
    hostname: str | None = None,
    capture: Literal[False] = False,
) -> int: ...


@overload
def run_restic(
    *args: str,
    config: BackupConfig,
    mount_paths: list[Path] | None = None,
    hostname: str | None = None,
    capture: Literal[True],
) -> tuple[int, str, str]: ...


def run_restic(
    *args: str,
    config: BackupConfig,
    mount_paths: list[Path] | None = None,
    hostname: str | None = None,
    capture: bool = False,
) -> int | tuple[int, str, str]:
    """Run a restic command inside a container (--rm).

    The password file is mounted into the container and referenced via
    RESTIC_PASSWORD_FILE — nothing sensitive appears on the command line.

    If capture is True, returns (exit_code, stdout, stderr) instead of
    just exit_code.
    """
    cmd = _build_restic_cmd(config)

    if hostname:
        cmd.extend(["-h", hostname])

    if mount_paths:
        mounted: set[str] = set()
        for path in mount_paths:
            path_str = str(path)
            if path_str not in mounted:
                cmd.extend(["-v", f"{path_str}:{path_str}:ro"])
                mounted.add(path_str)

    cmd.extend([config.restic_image, *args])
    log.debug("restic command: %s", " ".join(cmd))
    if capture:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    result = subprocess.run(cmd)
    return result.returncode


def repo_stats(config: BackupConfig) -> dict[str, Any]:
    exit_code, stdout, stderr = run_restic(
        "stats", "--json", config=config, capture=True,
    )
    if exit_code != 0:
        raise RuntimeError(
            f"restic stats failed (exit {exit_code}): {stderr}"
        )
    if not stdout.strip():
        return {}
    result: dict[str, Any] = json.loads(stdout)
    return result


def list_snapshots(
    config: BackupConfig, tag: str | None = None,
) -> list[dict[str, Any]]:
    args = ["snapshots", "--json"]
    if tag:
        args.extend(["--tag", tag])
    exit_code, stdout, stderr = run_restic(*args, config=config, capture=True)
    if exit_code != 0:
        raise RuntimeError(
            f"restic snapshots failed (exit {exit_code}): {stderr}"
        )
    if not stdout.strip():
        return []
    parsed = json.loads(stdout)
    if parsed is None:
        return []
    snapshots: list[dict[str, Any]] = parsed
    return snapshots


def iter_snapshot_files(
    config: BackupConfig, snapshot_id: str,
) -> Generator[dict[str, Any], None, None]:
    cmd = _build_restic_cmd(config)
    cmd.extend([config.restic_image, "ls", "--json", snapshot_id])
    with subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as proc:
        if proc.stdout is None:
            raise RuntimeError("Failed to capture stdout from restic ls")
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("struct_type") == "node":
                yield obj
        if proc.stderr is None:
            raise RuntimeError("Failed to capture stderr from restic ls")
        stderr = proc.stderr.read()
        exit_code = proc.wait()
    if exit_code != 0:
        raise RuntimeError(
            f"restic ls failed (exit {exit_code}): {stderr.strip()}"
        )


def restore_snapshot(
    config: BackupConfig, snapshot_id: str, target: str,
    dry_run: bool = False,
) -> int:
    cmd = _build_restic_cmd(config)
    cmd.extend(["-v", f"{target}:{target}"])
    args = ["restore", snapshot_id, "--target", target]
    if dry_run:
        args.append("--dry-run")
    cmd.extend([config.restic_image, *args])
    log.debug("restic command: %s", " ".join(cmd))
    result = subprocess.run(cmd)
    return result.returncode


def forget_snapshots(
    config: BackupConfig, snapshot_ids: list[str],
) -> int:
    if not snapshot_ids:
        return 0
    return run_restic("forget", *snapshot_ids, config=config)


def prune(config: BackupConfig) -> int:
    return run_restic("prune", config=config)


def diff_snapshots(
    config: BackupConfig, id1: str, id2: str,
) -> tuple[int, str, str]:
    exit_code, stdout, stderr = run_restic(
        "diff", id1, id2, config=config, capture=True,
    )
    return exit_code, stdout, stderr


def run_scope_backup(
    tag: str, paths: list[Path], exclude: list[str],
    config: BackupConfig, hostname: str | None = None,
) -> int:
    if not paths:
        return 0

    args: list[str] = ["backup", "--tag", tag]
    for pattern in exclude:
        args.extend(["--exclude", pattern])
    args.extend(str(p) for p in paths)

    log.info("  restic backup --tag %s (%d paths)", tag, len(paths))
    for path in paths:
        log.info("    %s", path)

    return run_restic(*args, config=config, mount_paths=paths, hostname=hostname)
