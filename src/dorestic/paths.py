from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

from dorestic.models import ContainerTarget

log = logging.getLogger("backup")


def parse_comma_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def resolve_host_path_spec(
    compose_dir: str,
    spec: str,
) -> tuple[Path, int | None]:
    """Parse a host path spec like '../config@2' into (resolved_path, max_depth)."""
    depth: int | None = None
    m = re.fullmatch(r"(.+)@(\d+)", spec)
    if m:
        spec = m.group(1)
        depth = int(m.group(2))

    resolved = Path(os.path.realpath(Path(compose_dir) / spec))
    return resolved, depth


def expand_depth_limited_path(base: Path, max_depth: int) -> list[Path]:
    result = subprocess.run(
        ["find", str(base), "-maxdepth", str(max_depth), "-type", "f"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.warning("find failed for %s: %s", base, result.stderr.strip())
        return []
    return [Path(line) for line in result.stdout.strip().splitlines() if line]


def resolve_host_paths(target: ContainerTarget) -> list[Path]:
    if not target.host_scope or not target.compose_dir:
        return []

    resolved: list[Path] = []
    for spec in target.host_scope.paths:
        path, depth = resolve_host_path_spec(target.compose_dir, spec)
        if depth is not None:
            expanded = expand_depth_limited_path(path, depth)
            log.debug(
                "%s: host spec %s → %s (@%d, %d files)",
                target.name, spec, path, depth, len(expanded),
            )
            resolved.extend(expanded)
        elif path.exists():
            log.debug("%s: host spec %s → %s", target.name, spec, path)
            resolved.append(path)
        else:
            log.warning("%s: host path %s does not exist", target.name, path)

    return resolved


