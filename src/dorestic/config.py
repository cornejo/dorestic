from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from dorestic.models import (
    BackupConfig,
    DEFAULT_RESTIC_IMAGE,
    DEFAULT_STALE_THRESHOLD_HOURS,
    HostGroup,
    RetentionPolicy,
)

CONFIG_FILENAME = "config.yml"

KNOWN_TOP_LEVEL_KEYS = frozenset({
    "repository", "password_file", "restic_image",
    "on_start", "on_complete", "retention",
    "stale_threshold_hours", "host_groups", "log_dir", "tmp_dir",
})
KNOWN_RETENTION_KEYS = frozenset({"daily", "weekly", "monthly"})
KNOWN_HOST_GROUP_KEYS = frozenset({
    "tag", "paths", "exclude", "on_start", "on_complete",
})


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
        f"Run 'dorestic init' to create one, or pass --config: dorestic --config /path/to/config.yml"
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

    stale_threshold_hours = int(
        data.get("stale_threshold_hours", DEFAULT_STALE_THRESHOLD_HOURS)
    )

    log_dir_val = data.get("log_dir")
    tmp_dir_val = data.get("tmp_dir")

    tmp_dir = str(tmp_dir_val) if tmp_dir_val is not None else "/tmp"
    tmp_path = Path(tmp_dir)
    if not tmp_path.exists():
        raise ValueError(f"tmp_dir does not exist: {tmp_dir}")
    if not tmp_path.is_dir():
        raise ValueError(f"tmp_dir is not a directory: {tmp_dir}")

    return BackupConfig(
        repository=str(data["repository"]),
        password_file=str(data["password_file"]),
        restic_image=str(data.get("restic_image", DEFAULT_RESTIC_IMAGE)),
        on_start=str(on_start_val) if on_start_val is not None else None,
        on_complete=str(on_complete_val) if on_complete_val is not None else None,
        retention=retention,
        host_groups=host_groups,
        stale_threshold_hours=stale_threshold_hours,
        log_dir=str(log_dir_val) if log_dir_val is not None else None,
        tmp_dir=str(tmp_dir_val) if tmp_dir_val is not None else "/tmp",
    )


def validate_raw_config(data: dict[str, Any]) -> None:
    """Validate that all keys in a raw config dict are known.

    Raises ValueError listing any unrecognized keys.
    """
    unknown = set(data.keys()) - KNOWN_TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(
            f"Unknown config keys: {', '.join(sorted(unknown))}"
        )

    raw_retention = data.get("retention")
    if raw_retention is not None and hasattr(raw_retention, "keys"):
        ret_unknown = set(raw_retention.keys()) - KNOWN_RETENTION_KEYS
        if ret_unknown:
            raise ValueError(
                f"Unknown retention keys: {', '.join(sorted(ret_unknown))}"
            )

    for group in data.get("host_groups", []):
        if not hasattr(group, "keys"):
            continue
        group_unknown = set(group.keys()) - KNOWN_HOST_GROUP_KEYS
        if group_unknown:
            tag = group.get("tag", "?")
            raise ValueError(
                f"Unknown keys in host group '{tag}': "
                f"{', '.join(sorted(group_unknown))}"
            )


def _yaml_str(value: str) -> str:
    """Format a string with YAML quoting if needed."""
    dumped = yaml.dump(value)
    if dumped.endswith("\n...\n"):
        dumped = dumped[:-5]
    return dumped.strip()


def render_config(data: dict[str, Any]) -> str:
    """Render a config dict as a documented YAML string."""
    lines: list[str] = []

    lines.append("# Restic backup configuration")
    lines.append("#")
    lines.append("# Edit this file and fill in your values.")
    lines.append("")

    lines.append("# Path to the restic repository (required)")
    lines.append(f"repository: {_yaml_str(str(data['repository']))}")
    lines.append("")

    lines.append(
        "# Path to a file containing the restic repository password (required)"
    )
    lines.append(
        "# This is mounted into the restic container via RESTIC_PASSWORD_FILE —"
    )
    lines.append("# the password never appears on the command line.")
    lines.append("# A trailing newline is fine (restic strips it).")
    lines.append(f"password_file: {_yaml_str(str(data['password_file']))}")
    lines.append("")

    lines.append(
        "# Restic Docker image (optional, default: restic/restic:latest)"
    )
    if "restic_image" in data:
        lines.append(f"restic_image: {_yaml_str(str(data['restic_image']))}")
    else:
        lines.append("# restic_image: restic/restic:latest")
    lines.append("")

    lines.append("# Command to run before the backup starts (optional)")
    lines.append("# If it exits non-zero, the entire backup is aborted.")
    lines.append("# All on_start/on_complete hooks are run via sh -c.")
    if "on_start" in data:
        lines.append(f"on_start: {_yaml_str(str(data['on_start']))}")
    else:
        lines.append("# on_start: /path/to/on_start.sh")
    lines.append("")

    lines.append(
        "# Command to run after the entire backup completes (optional)"
    )
    lines.append("# Environment: $DORESTIC_EXIT_CODE, $DORESTIC_LOGFILE")
    if "on_complete" in data:
        lines.append(f"on_complete: {_yaml_str(str(data['on_complete']))}")
    else:
        lines.append("# on_complete: /path/to/on_complete.sh")
    lines.append("")

    lines.append(
        "# Directory for persistent backup logs (optional)"
    )
    lines.append(
        "# Each run writes a timestamped log file (e.g. backup-2026-07-09T030000.log)."
    )
    lines.append(
        "# Without this, a temporary log is created for on_complete and then deleted."
    )
    if "log_dir" in data:
        lines.append(f"log_dir: {_yaml_str(str(data['log_dir']))}")
    else:
        lines.append("# log_dir: /var/log/dorestic")
    lines.append("")

    lines.append(
        "# Directory for temporary files during backup, verify, and restore (optional)"
    )
    lines.append(
        "# Defaults to /tmp, which is often a tmpfs (RAM-backed) on Linux."
    )
    lines.append(
        "# If you work with large backups, point this at a disk-backed path"
    )
    lines.append(
        "# that only your user can access (e.g. /var/tmp/dorestic)."
    )
    lines.append(
        "# The directory must already exist — dorestic will not create it."
    )
    if "tmp_dir" in data:
        lines.append(f"tmp_dir: {_yaml_str(str(data['tmp_dir']))}")
    else:
        lines.append("# tmp_dir: /var/tmp/dorestic")
    lines.append("")

    lines.append(
        "# Snapshot retention policy (optional, these are the defaults)"
    )
    if "retention" in data:
        ret = data["retention"]
        lines.append("retention:")
        lines.append(f"  daily: {ret.get('daily', 7)}")
        lines.append(f"  weekly: {ret.get('weekly', 4)}")
        lines.append(f"  monthly: {ret.get('monthly', 12)}")
    else:
        lines.append("# retention:")
        lines.append("#   daily: 7")
        lines.append("#   weekly: 4")
        lines.append("#   monthly: 12")
    lines.append("")

    lines.append(
        "# Hours after which a tag's latest snapshot is considered stale "
        "(optional, default: 25)"
    )
    lines.append(
        "# Used by `dorestic list` to flag tags that haven't been backed up "
        "recently."
    )
    if "stale_threshold_hours" in data:
        lines.append(f"stale_threshold_hours: {data['stale_threshold_hours']}")
    else:
        lines.append("# stale_threshold_hours: 25")
    lines.append("")

    lines.append(
        "# Host-only backup groups — not tied to any Docker container (optional)"
    )
    lines.append("# Each group gets its own tagged restic snapshot.")
    if "host_groups" in data and data["host_groups"]:
        lines.append("host_groups:")
        for group in data["host_groups"]:
            lines.append(f"  - tag: {_yaml_str(str(group['tag']))}")
            lines.append("    paths:")
            for p in group["paths"]:
                lines.append(f"      - {_yaml_str(str(p))}")
            if group.get("exclude"):
                lines.append("    exclude:")
                for e in group["exclude"]:
                    lines.append(f"      - {_yaml_str(str(e))}")
            if group.get("on_start"):
                lines.append(
                    f"    on_start: {_yaml_str(str(group['on_start']))}"
                )
            if group.get("on_complete"):
                lines.append(
                    f"    on_complete: {_yaml_str(str(group['on_complete']))}"
                )
    else:
        lines.append("# host_groups:")
        lines.append("#   - tag: documents")
        lines.append("#     paths:")
        lines.append("#       - /mnt/fileserver/share")
        lines.append("#     exclude:")
        lines.append('#       - "*.tmp"')
    lines.append("")

    return "\n".join(lines)


def refresh_config(config_path: str) -> str:
    """Refresh an existing config file with the latest template.

    Validates all keys, renders the new config, rotates the old file to .bak,
    and writes the new config in place.

    Returns the path to the .bak file.
    Raises ValueError on unknown config keys.
    """
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    data: dict[str, Any] = _as_dict(
        raw, f"Config file {config_path} must be a YAML mapping",
    )
    validate_raw_config(data)

    new_content = render_config(data)

    path = Path(config_path)
    bak_path = path.with_suffix(".yml.bak")
    if bak_path.exists():
        bak_path.unlink()
    path.rename(bak_path)
    path.write_text(new_content)

    return str(bak_path)
