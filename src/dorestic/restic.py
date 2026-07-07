from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Literal, overload

from dorestic.models import BackupConfig

log = logging.getLogger("backup")


@overload
def run_restic(
    *args: str,
    config: BackupConfig,
    mount_paths: list[Path] | None = None,
    capture: Literal[False] = False,
) -> int: ...


@overload
def run_restic(
    *args: str,
    config: BackupConfig,
    mount_paths: list[Path] | None = None,
    capture: Literal[True],
) -> tuple[int, str]: ...


def run_restic(
    *args: str,
    config: BackupConfig,
    mount_paths: list[Path] | None = None,
    capture: bool = False,
) -> int | tuple[int, str]:
    """Run a restic command inside a container (--rm).

    The password file is mounted into the container and referenced via
    RESTIC_PASSWORD_FILE — nothing sensitive appears on the command line.

    If capture is True, returns (exit_code, combined_output) instead of
    just exit_code.
    """
    password_mount = "/run/secrets/restic-password"
    cmd: list[str] = [
        "docker", "run", "--rm",
        "-e", f"RESTIC_REPOSITORY={config.repository}",
        "-e", f"RESTIC_PASSWORD_FILE={password_mount}",
        "-v", f"{config.password_file}:{password_mount}:ro",
    ]

    if Path(config.repository).is_absolute():
        cmd.extend(["-v", f"{config.repository}:{config.repository}"])

    if mount_paths:
        mounted: set[str] = set()
        for path in mount_paths:
            path_str = str(path)
            if path_str not in mounted:
                cmd.extend(["-v", f"{path_str}:{path_str}:ro"])
                mounted.add(path_str)

    cmd.extend([config.restic_image, *args])
    if capture:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode, (result.stdout + result.stderr).strip()
    result = subprocess.run(cmd)
    return result.returncode


def run_scope_backup(
    tag: str, paths: list[Path], exclude: list[str],
    config: BackupConfig,
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

    return run_restic(*args, config=config, mount_paths=paths)
