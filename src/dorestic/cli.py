from __future__ import annotations

import argparse
import importlib.resources
import sys
from pathlib import Path

from dorestic.config import find_config


def write_example_config(dest: str) -> None:
    """Write the bundled config.yml.example to the given path."""
    dest_path = Path(dest)

    if dest_path.is_dir():
        dest_path = dest_path / "config.yml"

    if dest_path.exists():
        print(f"Error: {dest_path} already exists", file=sys.stderr)
        sys.exit(1)

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    ref = importlib.resources.files("dorestic").joinpath("config.yml.example")
    content = ref.read_text(encoding="utf-8")
    dest_path.write_text(content)
    print(f"Wrote example config to {dest_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="dorestic",
        description="Label-driven Docker backup using restic.",
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=None,
        help="path to config.yml (default: auto-discover)",
    )
    parser.add_argument(
        "--init",
        metavar="PATH",
        nargs="?",
        const=".",
        help="write example config.yml to PATH (default: current directory)",
    )

    args = parser.parse_args()

    if args.init is not None:
        write_example_config(args.init)
        return

    config_path: str = args.config if args.config is not None else find_config()

    from dorestic.backup import run_backup
    run_backup(config_path)
