from __future__ import annotations

import argparse
import importlib.resources
import sys
from datetime import datetime, timezone
from pathlib import Path

from dorestic.api import Dorestic
from dorestic.config import find_config
from dorestic.display import (
    format_size,
    print_dry_run_plan,
    print_status,
    print_tag_detail,
    print_tag_summary,
)


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
    d = Dorestic.from_config_path(_resolve_config(args))
    tag_filter: str | None = args.tag
    snapshots = d.list_snapshots(tag=tag_filter)

    if not snapshots:
        if tag_filter:
            print(f"No snapshots found for tag '{tag_filter}'")
        else:
            print("No snapshots found")
        return

    now = datetime.now(timezone.utc)

    if tag_filter:
        print_tag_detail(snapshots, now, d.config)
    else:
        print_tag_summary(snapshots, now, d.config)


def _cmd_view(args: argparse.Namespace) -> None:
    d = Dorestic.from_config_path(_resolve_config(args))
    snapshot_ref: str = args.snapshot

    snapshot = d.resolve_snapshot(snapshot_ref)
    if snapshot:
        tags_str = ", ".join(snapshot.tags or ["(untagged)"])
        time_str = snapshot.time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"Snapshot {snapshot.short_id} ({tags_str}) - {time_str}")
        print()
        lookup_id = snapshot.id
    else:
        lookup_id = snapshot_ref

    found = False
    for entry in d.iter_snapshot_files(lookup_id):
        found = True
        if entry.type == "dir":
            print(f"{entry.path}/")
        else:
            print(f"{entry.path}  ({format_size(entry.size)})")

    if not found:
        print("(no files)")


def _cmd_backup(args: argparse.Namespace) -> None:
    config_path = _resolve_config(args)
    if args.dry_run:
        d = Dorestic.from_config_path(config_path)
        plan = d.dry_run(only=args.only)
        print_dry_run_plan(plan)
        return
    from dorestic.backup import run_backup
    run_backup(config_path, only=args.only, verbose=args.verbose, quiet=args.quiet)


def _cmd_init(args: argparse.Namespace) -> None:
    if args.refresh:
        from dorestic.config import refresh_config
        config_path = _resolve_config(args)
        try:
            bak_path = refresh_config(config_path)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Refreshed {config_path}")
        print(f"Old config saved to {bak_path}")
    else:
        write_example_config(args.path)


def _cmd_status(args: argparse.Namespace) -> None:
    d = Dorestic.from_config_path(_resolve_config(args))
    report = d.status()
    now = datetime.now(timezone.utc)
    print_status(report, now)


def _cmd_check(args: argparse.Namespace) -> None:
    d = Dorestic.from_config_path(_resolve_config(args))
    ok = d.check()
    if ok:
        print("Repository integrity check passed.")
    else:
        print("Repository integrity check failed.", file=sys.stderr)
        sys.exit(1)


def _cmd_config_validate(args: argparse.Namespace) -> None:
    config_path = _resolve_config(args)
    try:
        from dorestic.config import validate_raw_config
        import yaml
        with open(config_path) as f:
            raw = yaml.safe_load(f)
        if not hasattr(raw, "keys"):
            print(f"Error: {config_path} is not a YAML mapping", file=sys.stderr)
            sys.exit(1)
        validate_raw_config(raw)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        d = Dorestic.from_config_path(config_path)
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    issues = d.validate()
    if issues:
        for issue in issues:
            print(f"Warning: {issue}", file=sys.stderr)

    print(f"Config: {config_path}")
    print(f"Repository: {d.config.repository}")
    print(f"Password file: {d.config.password_file}")
    if d.config.log_dir:
        print(f"Log dir: {d.config.log_dir}")
    print(
        f"Retention: {d.config.retention.daily} daily, "
        f"{d.config.retention.weekly} weekly, "
        f"{d.config.retention.monthly} monthly"
    )
    if d.config.host_groups:
        print(f"Host groups: {', '.join(g.tag for g in d.config.host_groups)}")

    if not issues:
        print("OK")
    else:
        sys.exit(1)


def _cmd_restore(args: argparse.Namespace) -> None:
    d = Dorestic.from_config_path(_resolve_config(args))
    target: str | None = args.target
    try:
        result = d.restore(args.snapshot, target=target, dry_run=args.dry_run)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        if result.success:
            print(f"Dry run: would restore {result.snapshot_id[:8]} to {result.target}")
        else:
            print("Dry run: restore would fail.", file=sys.stderr)
            sys.exit(1)
        return

    if result.success:
        print(f"Restored to {result.target}")
        print(f"  {result.file_count} files, {format_size(result.total_size)}")
    else:
        print("Restore failed.", file=sys.stderr)
        sys.exit(1)


def _cmd_verify(args: argparse.Namespace) -> None:
    d = Dorestic.from_config_path(_resolve_config(args))
    ref: str | None = args.snapshot
    try:
        result = d.verify_snapshot(ref=ref)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    tags_str = ", ".join(result.tags) if result.tags else "(untagged)"
    if result.success:
        print(f"Verified snapshot {result.snapshot_id[:8]} ({tags_str})")
        print(f"  {result.file_count} files, {format_size(result.total_size)}")
    else:
        print(
            f"Verification failed for snapshot {result.snapshot_id[:8]} ({tags_str})",
            file=sys.stderr,
        )
        sys.exit(1)


def _cmd_diff(args: argparse.Namespace) -> None:
    d = Dorestic.from_config_path(_resolve_config(args))
    try:
        result = d.diff(args.snapshot1, args.snapshot2)
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not result.entries:
        print("No differences.")
        return

    for entry in result.entries:
        print(f"{entry.modifier} {entry.path}")


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
    backup_parser.add_argument(
        "--dry-run", action="store_true",
        help="show what would be backed up without running anything",
    )
    verbosity = backup_parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-v", "--verbose", action="store_true",
        help="show debug-level output (resolved paths, restic commands)",
    )
    verbosity.add_argument(
        "-q", "--quiet", action="store_true",
        help="suppress output on success, print everything on failure",
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
        "init", help="write example config or refresh existing config",
    )
    init_parser.add_argument(
        "path", nargs="?", default=".",
        help="destination path (default: current directory)",
    )
    init_parser.add_argument(
        "--refresh", action="store_true",
        help="refresh existing config with latest template (old config saved as .bak)",
    )

    subparsers.add_parser(
        "status", help="show repository health: size, latest backups, retention",
    )

    subparsers.add_parser(
        "check", help="run a repository integrity check",
    )

    subparsers.add_parser(
        "config-validate",
        help="validate config and Docker labels without running a backup",
    )

    restore_parser = subparsers.add_parser(
        "restore",
        help="restore from a snapshot to a staging directory",
    )
    restore_parser.add_argument(
        "snapshot",
        help="snapshot ID or tag name (uses latest snapshot for tag)",
    )
    restore_parser.add_argument(
        "--target", "-t", default=None,
        help="target directory (default: ./restore/<tag>/)",
    )
    restore_parser.add_argument(
        "--dry-run", action="store_true",
        help="preview what would be restored without writing files",
    )

    verify_parser = subparsers.add_parser(
        "verify-snapshot",
        help="restore a snapshot to a temp dir to prove recoverability",
    )
    verify_parser.add_argument(
        "snapshot", nargs="?", default=None,
        help="snapshot ID or tag (default: random snapshot)",
    )

    diff_parser = subparsers.add_parser(
        "diff", help="show what changed between two snapshots",
    )
    diff_parser.add_argument("snapshot1", help="first snapshot ID or tag")
    diff_parser.add_argument("snapshot2", help="second snapshot ID or tag")

    args = parser.parse_args()

    commands = {
        "backup": _cmd_backup,
        "list": _cmd_list,
        "view": _cmd_view,
        "init": _cmd_init,
        "status": _cmd_status,
        "check": _cmd_check,
        "config-validate": _cmd_config_validate,
        "restore": _cmd_restore,
        "verify-snapshot": _cmd_verify,
        "diff": _cmd_diff,
    }
    commands[args.command](args)
