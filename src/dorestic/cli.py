from __future__ import annotations

import argparse
import importlib.resources
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dorestic.config import find_config, load_config
from dorestic.display import (
    format_size,
    parse_snapshot_time,
    print_tag_detail,
    print_tag_summary,
)
from dorestic.models import BackupConfig


def write_example_config(dest: str) -> None:
    """Write the bundled config.yml.example to the given path."""
    dest_path = Path(dest)

    if dest_path.suffix != ".yml":
        dest_path = dest_path / "config.yml"

    if dest_path.exists():
        print(f"Error: {dest_path} already exists", file=sys.stderr)
        sys.exit(1)

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    ref = importlib.resources.files("dorestic").joinpath("config.yml.example")
    content = ref.read_text(encoding="utf-8")
    dest_path.write_text(content)
    print(f"Wrote example config to {dest_path}")


def _resolve_config(args: argparse.Namespace) -> str:
    return args.config if args.config is not None else find_config()


def _cmd_list(args: argparse.Namespace) -> None:
    config_path = _resolve_config(args)
    config = load_config(config_path)

    from dorestic.restic import list_snapshots
    tag_filter: str | None = args.tag
    snapshots = list_snapshots(config, tag=tag_filter)

    if not snapshots:
        if tag_filter:
            print(f"No snapshots found for tag '{tag_filter}'")
        else:
            print("No snapshots found")
        return

    now = datetime.now(timezone.utc)

    if tag_filter:
        print_tag_detail(snapshots, now, config)
    else:
        print_tag_summary(snapshots, now, config)


def _cmd_view(args: argparse.Namespace) -> None:
    config_path = _resolve_config(args)
    config = load_config(config_path)

    from dorestic.restic import iter_snapshot_files

    snapshot_ref: str = args.snapshot

    snapshot_meta = _resolve_snapshot(config, snapshot_ref)
    if snapshot_meta:
        snap_id = snapshot_meta.get("short_id", snapshot_meta["id"][:8])
        tags = ", ".join(snapshot_meta.get("tags") or ["(untagged)"])
        snap_time = parse_snapshot_time(snapshot_meta["time"])
        time_str = snap_time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"Snapshot {snap_id} ({tags}) - {time_str}")
        print()
        lookup_id = snapshot_meta["id"]
    else:
        lookup_id = snapshot_ref

    found = False
    for entry in iter_snapshot_files(config, lookup_id):
        found = True
        path = entry.get("path", "")
        node_type = entry.get("type", "")
        if node_type == "dir":
            print(f"{path}/")
        else:
            size = entry.get("size", 0)
            print(f"{path}  ({format_size(size)})")

    if not found:
        print("(no files)")


def _resolve_snapshot(
    config: BackupConfig,
    ref: str,
) -> dict[str, Any] | None:
    from dorestic.restic import list_snapshots
    all_snaps = list_snapshots(config)
    best_tag_match: dict[str, Any] | None = None
    for snap in all_snaps:
        snap_id = snap.get("short_id", snap["id"][:8])
        if snap_id == ref or snap["id"] == ref:
            return snap
        tags: list[str] = snap.get("tags") or []
        if ref in tags:
            if best_tag_match is None or snap["time"] > best_tag_match["time"]:
                best_tag_match = snap
    return best_tag_match


def _cmd_backup(args: argparse.Namespace) -> None:
    config_path = _resolve_config(args)
    from dorestic.backup import run_backup
    run_backup(config_path, only=args.only)


def _cmd_init(args: argparse.Namespace) -> None:
    write_example_config(args.path)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="dorestic",
        description="Label-driven Docker backup using restic.",
    )
    parser.add_argument(
        "--config", "-c", default=None,
        help="path to config.yml (default: auto-discover)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="run a backup")
    backup_parser.add_argument(
        "--only", default=None,
        help="back up only this container or host group (skips global hooks, prune, and check)",
    )

    list_parser = subparsers.add_parser(
        "list", help="show snapshots grouped by tag with freshness",
    )
    list_parser.add_argument(
        "--tag", "-t", default=None,
        help="filter to a specific tag (shows individual snapshots)",
    )

    view_parser = subparsers.add_parser(
        "view", help="show files in a specific snapshot or latest for a tag",
    )
    view_parser.add_argument(
        "snapshot",
        help="snapshot ID or tag name (uses latest snapshot for tag)",
    )

    init_parser = subparsers.add_parser(
        "init", help="write example config.yml",
    )
    init_parser.add_argument(
        "path", nargs="?", default=".",
        help="destination path (default: current directory)",
    )

    args = parser.parse_args()

    commands = {
        "backup": _cmd_backup,
        "list": _cmd_list,
        "view": _cmd_view,
        "init": _cmd_init,
    }
    commands[args.command](args)
