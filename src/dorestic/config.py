from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from dorestic.models import (
    BackupConfig,
    DEFAULT_RESTIC_IMAGE,
    HostGroup,
    RetentionPolicy,
)

CONFIG_FILENAME = "config.yml"


def find_config() -> str:
    """Search for config.yml in standard locations.

    Order: ./config.yml, then $XDG_CONFIG_HOME/dorestic/config.yml
    (defaulting to ~/.config/dorestic/config.yml).
    """
    local = Path(CONFIG_FILENAME)
    if local.is_file():
        return str(local)

    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    config_dir = Path(xdg) if xdg else Path.home() / ".config"
    xdg_path = config_dir / "dorestic" / CONFIG_FILENAME
    if xdg_path.is_file():
        return str(xdg_path)

    raise FileNotFoundError(
        f"No config.yml found. Searched:\n"
        f"  ./{CONFIG_FILENAME}\n"
        f"  {xdg_path}\n"
        f"Run 'dorestic --init' to create one, or pass a path: dorestic /path/to/config.yml"
    )


def _as_dict(value: Any, msg: str) -> dict[str, Any]:
    """Validate that a YAML value is a mapping and return it typed."""
    if not hasattr(value, "keys"):
        raise ValueError(msg)
    result: dict[str, Any] = value
    return result


def load_config(path: str) -> BackupConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)

    data: dict[str, Any] = _as_dict(raw, f"Config file {path} must be a YAML mapping")

    if "repository" not in data:
        raise ValueError(f"Config file {path}: missing required field 'repository'")
    if "password_file" not in data:
        raise ValueError(f"Config file {path}: missing required field 'password_file'")
    if "excludes" in data:
        raise ValueError("Config uses 'excludes' (plural) — use 'exclude' instead")

    pw_path = Path(str(data["password_file"]))
    if not pw_path.exists():
        raise ValueError(f"password_file does not exist: {pw_path}")

    retention = RetentionPolicy()
    raw_retention = data.get("retention")
    if raw_retention is not None:
        ret = _as_dict(raw_retention, "retention must be a mapping")
        if "daily" in ret:
            retention.daily = int(ret["daily"])
        if "weekly" in ret:
            retention.weekly = int(ret["weekly"])
        if "monthly" in ret:
            retention.monthly = int(ret["monthly"])

    host_groups: list[HostGroup] = []
    for entry in data.get("host_groups", []):
        group_data: dict[str, Any] = entry
        if "excludes" in group_data:
            raise ValueError(
                f"host_groups entry '{group_data.get('tag', '?')}' uses 'excludes' "
                "(plural) — use 'exclude' instead"
            )
        on_start_val = group_data.get("on_start")
        on_complete_val = group_data.get("on_complete")
        host_groups.append(
            HostGroup(
                tag=str(group_data["tag"]),
                paths=[str(p) for p in group_data["paths"]],
                exclude=[str(e) for e in group_data.get("exclude", [])],
                on_start=str(on_start_val) if on_start_val is not None else None,
                on_complete=str(on_complete_val) if on_complete_val is not None else None,
            )
        )

    on_start_val = data.get("on_start")
    on_complete_val = data.get("on_complete")

    return BackupConfig(
        repository=str(data["repository"]),
        password_file=str(data["password_file"]),
        restic_image=str(data.get("restic_image", DEFAULT_RESTIC_IMAGE)),
        on_start=str(on_start_val) if on_start_val is not None else None,
        on_complete=str(on_complete_val) if on_complete_val is not None else None,
        retention=retention,
        host_groups=host_groups,
    )
