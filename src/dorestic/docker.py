from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import docker
from docker.models.containers import Container

from dorestic.models import (
    DEFAULT_LABEL_PREFIX,
    ContainerTarget,
    ScopeConfig,
)
from dorestic.paths import parse_comma_list

log = logging.getLogger("backup")


def _get_container_labels(container: Container) -> dict[str, str]:
    """Extract labels with proper typing via attrs (stubs lack labels annotation)."""
    labels: dict[str, str] = container.attrs.get("Config", {}).get("Labels") or {}
    return labels


def _get_container_mounts(container: Container) -> list[dict[str, str]]:
    """Extract mount info from a container."""
    mounts: list[dict[str, str]] = container.attrs.get("Mounts") or []
    return mounts


def _get_container_name(container: Container) -> str:
    return container.name or container.short_id


def run_docker_exec(container: Container, command: str) -> tuple[int, str]:
    result = container.exec_run(["sh", "-c", command])
    exit_code = result.exit_code if result.exit_code is not None else -1
    output = result.output
    if isinstance(output, bytes):
        return exit_code, output.decode().strip()
    return exit_code, ""


def resolve_container_path(
    container: Container,
    container_path: str,
    suppress_warning: bool,
) -> Path | None:
    mounts = _get_container_mounts(container)

    best_mount: dict[str, str] | None = None
    best_length = -1

    for mount in mounts:
        destination = mount.get("Destination", "")
        is_match = container_path == destination or container_path.startswith(
            destination + "/"
        )
        if is_match and len(destination) > best_length:
            best_mount = mount
            best_length = len(destination)

    if best_mount is None:
        if not suppress_warning:
            log.warning(
                "%s: path %s has no matching mount",
                _get_container_name(container),
                container_path,
            )
        return None

    relative = container_path[len(best_mount.get("Destination", "")):]
    host_path = best_mount.get("Source", "") + relative
    return Path(host_path)


def docker_cp(container: Container, container_path: str, staging_dir: Path) -> Path | None:
    """Copy a path from a container to a staging directory via docker cp."""
    dest = staging_dir / _get_container_name(container) / container_path.lstrip("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    container_id = container.short_id
    result = subprocess.run(
        ["docker", "cp", f"{container_id}:{container_path}", str(dest)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.error(
            "%s: docker cp %s failed: %s",
            _get_container_name(container),
            container_path,
            result.stderr.strip(),
        )
        return None
    return dest


def resolve_container_paths(
    target: ContainerTarget,
    staging_dir: Path | None = None,
) -> list[Path]:
    if not target.container_scope:
        return []

    resolved: list[Path] = []
    for path_str in target.container_scope.paths:
        path = resolve_container_path(
            target.container,
            path_str,
            target.suppress_mount_warning,
        )
        if path is not None:
            if not path.exists():
                log.warning("%s: resolved path %s does not exist", target.name, path)
                continue
            resolved.append(path)
            continue
        if staging_dir is not None:
            if not target.suppress_mount_warning:
                log.warning(
                    "%s: path %s is not on a volume mount, using docker cp",
                    target.name,
                    path_str,
                )
            staged = docker_cp(target.container, path_str, staging_dir)
            if staged is not None:
                resolved.append(staged)
    return resolved


def discover_targets(
    client: docker.DockerClient,
    label_prefix: str = DEFAULT_LABEL_PREFIX,
) -> list[ContainerTarget]:
    containers: list[Container] = client.containers.list(
        filters={"label": f"{label_prefix}.enable=true"}
    )
    targets: list[ContainerTarget] = []

    for container in containers:
        labels = _get_container_labels(container)
        name = _get_container_name(container)

        for scope in ("container", "host"):
            bad_key = f"{label_prefix}.{scope}.excludes"
            if bad_key in labels:
                raise ValueError(
                    f"{name}: label '{bad_key}' uses 'excludes' (plural) "
                    f"— use '{label_prefix}.{scope}.exclude' instead"
                )

        container_scope: ScopeConfig | None = None
        raw_container_paths = labels.get(f"{label_prefix}.container.paths", "")
        if raw_container_paths:
            container_scope = ScopeConfig(
                paths=parse_comma_list(raw_container_paths),
                exclude=parse_comma_list(
                    labels.get(f"{label_prefix}.container.exclude", "")
                ),
                on_start=labels.get(f"{label_prefix}.container.on_start"),
                on_complete=labels.get(f"{label_prefix}.container.on_complete"),
            )

        host_scope: ScopeConfig | None = None
        raw_host_paths = labels.get(f"{label_prefix}.host.paths", "")
        if raw_host_paths:
            host_scope = ScopeConfig(
                paths=parse_comma_list(raw_host_paths),
                exclude=parse_comma_list(
                    labels.get(f"{label_prefix}.host.exclude", "")
                ),
                on_start=labels.get(f"{label_prefix}.host.on_start"),
                on_complete=labels.get(f"{label_prefix}.host.on_complete"),
            )

        compose_dir = labels.get("com.docker.compose.project.working_dir")

        suppress_key = f"{label_prefix}.suppress-mount-warning"
        if not container_scope and not host_scope and not compose_dir:
            log.info(
                "%s: no paths configured and no compose dir, skipping",
                name,
            )
            continue

        targets.append(
            ContainerTarget(
                name=name,
                container=container,
                container_scope=container_scope,
                host_scope=host_scope,
                suppress_mount_warning=labels.get(suppress_key) == "true",
                compose_dir=compose_dir,
            )
        )

    return targets
