from __future__ import annotations

import hashlib
import json
import logging
import re
import os
import subprocess
import sys
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any, Literal, overload

from dorestic.models import EXIT_UNMOUNTABLE_PATH, BackupConfig, BackupPath

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


def _run_streaming(cmd: list[str]) -> int:
    """Run a command, relaying its output through sys.stdout.

    subprocess cannot write to a TeeStream directly — it needs a real file
    descriptor — so without this the restic output would bypass the log file
    and `backup -q` entirely, going straight to the inherited fd 1/2.
    stderr is merged into stdout to keep the log chronological.
    """
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    ) as proc:
        if proc.stdout is None:
            raise RuntimeError("Failed to capture output from restic")
        for line in proc.stdout:
            sys.stdout.write(line)
        sys.stdout.flush()
        return proc.wait()


def as_backup_paths(paths: Sequence[Path | BackupPath]) -> list[BackupPath]:
    """Treat a bare Path as a local path mounted at its own location.

    Host-scope paths are resolved in our own namespace and are already stable,
    so they need neither remapping nor namespace guarding.
    """
    return [
        p if isinstance(p, BackupPath) else BackupPath(source=p, target=p)
        for p in paths
    ]


def _covers_as_dir(path: Path) -> bool:
    """Whether `path` can be bind-mounted as-is rather than via its parent.

    A directory covers itself. A plain file does not — mounting its parent is
    what keeps the mount count down — but an *unreadable* path must be treated
    as a directory: a failed stat is not evidence that the path is a file, and
    guessing "file" here would bind-mount the parent instead, widening what is
    exposed to restic on the strength of an error. Binding a file directly
    works, so the conservative answer is also the safe one.
    """
    try:
        return path.is_dir()
    except OSError:
        return True


def _collapse_mounts(paths: list[BackupPath]) -> list[BackupPath]:
    """Reduce a path list to the smallest set of bind mounts covering it.

    A depth-limited host spec (`dir@2`) expands to one entry per file, and a
    bind mount each would overflow the command line on any real tree. Mounting
    the containing directories instead covers them in a handful of mounts;
    restic still reads only the paths it is given as arguments.

    Collapsing is only meaningful *within one mount namespace*: a prefix
    relationship between paths from different namespaces says nothing about
    where the data is, and a daemon-sourced path cannot be stat'd here at all.
    So only local, identity-mapped paths are collapsed; anything daemon-sourced
    or remapped to a pinned target passes through untouched.

    Idempotent, so a caller may collapse first to inspect the result and pass
    it on: a collapsed local entry is a directory, which covers itself.
    """
    collapsible = [p for p in paths if not p.daemon_sourced and not p.remapped]
    passthrough = [p for p in paths if p.daemon_sourced or p.remapped]

    dirs: set[Path] = set()
    for p in collapsible:
        dirs.add(p.source if _covers_as_dir(p.source) else p.source.parent)

    minimal: list[Path] = []
    # Shortest paths first, so an ancestor is always seen before its children.
    for candidate in sorted(dirs, key=lambda p: (len(p.parts), str(p))):
        if not any(candidate == m or m in candidate.parents for m in minimal):
            minimal.append(candidate)

    mounts = [BackupPath(source=p, target=p) for p in minimal]
    seen = {(str(m.source), str(m.target)) for m in mounts}
    for p in passthrough:
        spec = (str(p.source), str(p.target))
        if spec not in seen:
            seen.add(spec)
            mounts.append(p)
    return mounts


def _provably_present(mount: BackupPath) -> bool:
    """Whether this source is known to exist, so `-v` cannot auto-create it.

    Only answerable for a local source. A daemon-sourced path is not ours to
    stat — the answer would be about our namespace, not the daemon's — so it
    is never "provably" anything from here.
    """
    if mount.daemon_sourced:
        return False
    try:
        os.stat(mount.source)
    except OSError:
        return False
    return True


def unmountable_paths(mounts: list[BackupPath]) -> list[str]:
    """Sources that cannot be bind-mounted safely, as human-readable reasons.

    Only commas, for now: `--mount` parses its value as CSV and has no way to
    express one, quoted or otherwise, so such a path has to go through `-v`.

    `-v` is only dangerous because it *creates* a missing source rather than
    failing — so where the source is already known to exist, that danger is
    excluded and `-v` is provably safe. A comma in a local, verifiable path has
    always worked and keeps working; the strictness lands only where the
    uncertainty actually is, on a source we cannot check.
    """
    return [
        f"{m.source}: path contains a comma, which docker --mount cannot "
        f"express, and the source cannot be verified to exist"
        for m in mounts
        if ("," in str(m.source) or "," in str(m.target))
        and not _provably_present(m)
    ]


def _mount_args(mount: BackupPath) -> list[str]:
    """Bind this source read-only at its target inside the restic container.

    `--mount` rather than `-v` because `-v` *creates* a missing source as an
    empty root-owned directory and carries on, which turns a bad path into a
    silently empty backup. `--mount` fails the run instead.
    """
    source, target = str(mount.source), str(mount.target)
    if "," not in source and "," not in target:
        return ["--mount", f"type=bind,src={source},dst={target},readonly"]
    if not _provably_present(mount):
        # Guarded by unmountable_paths() before restic is ever invoked; this
        # is the backstop that keeps a new caller from bypassing that check.
        raise ValueError(
            f"cannot bind-mount an unverifiable path containing a comma: {source}"
        )
    # --mount cannot express the comma, but the source is known to exist, so
    # the auto-create that makes -v unsafe cannot happen here.
    return ["-v", f"{source}:{target}:ro"]


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
    mount_paths: Sequence[Path | BackupPath] | None = None,
    hostname: str | None = None,
    capture: Literal[False] = False,
) -> int: ...


@overload
def run_restic(
    *args: str,
    config: BackupConfig,
    mount_paths: Sequence[Path | BackupPath] | None = None,
    hostname: str | None = None,
    capture: Literal[True],
) -> tuple[int, str, str]: ...


def run_restic(
    *args: str,
    config: BackupConfig,
    mount_paths: Sequence[Path | BackupPath] | None = None,
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
        for mount in _collapse_mounts(as_backup_paths(mount_paths)):
            cmd.extend(_mount_args(mount))

    cmd.extend([config.restic_image, *args])
    log.debug("restic command: %s", " ".join(cmd))
    if capture:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    return _run_streaming(cmd)


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
    return _run_streaming(cmd)


def docker_rmtree(config: BackupConfig, path: str) -> None:
    """Remove a directory tree using Docker to handle root-owned files.

    Restic restores inside Docker create root-owned files that a non-root
    host user cannot delete.  This runs rm -rf inside a container with the
    same image that created the files.
    """
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{path}:{path}",
        "--entrypoint", "rm",
        config.restic_image,
        "-rf", path,
    ]
    log.debug("docker rmtree: %s", " ".join(cmd))
    subprocess.run(cmd, capture_output=True)


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
    tag: str, paths: Sequence[Path | BackupPath], exclude: list[str],
    config: BackupConfig, hostname: str | None = None,
) -> int:
    if not paths:
        return 0

    mounts = as_backup_paths(paths)

    unmountable = unmountable_paths(_collapse_mounts(mounts))
    if unmountable:
        for reason in unmountable:
            log.error("  cannot back up %s", reason)
        return EXIT_UNMOUNTABLE_PATH

    # restic is given the *target* paths — where the data appears inside the
    # restic container — because those are what it records in the snapshot.
    # For a container scope the target is pinned to the container's own path,
    # so the snapshot path stays put even when the source does not.
    args: list[str] = ["backup", "--tag", tag]
    for pattern in exclude:
        args.extend(["--exclude", pattern])
    args.extend(str(m.target) for m in mounts)

    log.info("  restic backup --tag %s (%d paths)", tag, len(mounts))
    for mount in mounts:
        if mount.remapped:
            log.info("    %s (from %s)", mount.target, mount.source)
        else:
            log.info("    %s", mount.target)

    return run_restic(*args, config=config, mount_paths=mounts, hostname=hostname)
