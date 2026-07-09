from __future__ import annotations

from datetime import datetime

from dorestic.models import BackupConfig, DryRunPlan, DryRunScope, Snapshot


def format_freshness(dt: datetime, now: datetime) -> str:
    delta = now - dt
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "just now"

    minutes = total_seconds // 60
    hours = total_seconds // 3600
    days = total_seconds // 86400

    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    if hours < 24:
        return f"{hours}h ago"
    if days == 1:
        return "1d ago"
    return f"{days}d ago"


def is_stale(dt: datetime, now: datetime, threshold_hours: int) -> bool:
    hours_ago = (now - dt).total_seconds() / 3600
    return hours_ago >= threshold_hours


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KiB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MiB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GiB"


def _print_scope(scope: DryRunScope, indent: str = "  ") -> None:
    if scope.paths:
        for p in scope.paths:
            print(f"{indent}{p}")
    else:
        print(f"{indent}(no paths resolved)")
    if scope.exclude:
        print(f"{indent}exclude: {', '.join(scope.exclude)}")
    if scope.on_start:
        print(f"{indent}on_start: {scope.on_start}")
    if scope.on_complete:
        print(f"{indent}on_complete: {scope.on_complete}")


def print_dry_run_plan(plan: DryRunPlan) -> None:
    if plan.global_on_start:
        print(f"global on_start: {plan.global_on_start}")
    if plan.global_on_complete:
        print(f"global on_complete: {plan.global_on_complete}")
    if plan.global_on_start or plan.global_on_complete:
        print()

    if not plan.targets and not plan.host_groups:
        print("Nothing to back up.")
        return

    for target in plan.targets:
        print(target.name)
        if target.container_scope:
            print(f"  container ({target.container_scope.tag})")
            _print_scope(target.container_scope, indent="    ")
        if target.host_scope:
            print(f"  host ({target.host_scope.tag})")
            _print_scope(target.host_scope, indent="    ")
        print()

    for group in plan.host_groups:
        print(f"host:{group.tag}")
        _print_scope(group)
        print()


def print_tag_summary(
    snapshots: list[Snapshot], now: datetime, config: BackupConfig,
) -> None:
    by_tag: dict[str, list[Snapshot]] = {}
    for snap in snapshots:
        tags = snap.tags or ["(untagged)"]
        for tag in tags:
            by_tag.setdefault(tag, []).append(snap)

    rows: list[tuple[str, int, datetime, str, bool]] = []
    for tag in sorted(by_tag):
        snaps = by_tag[tag]
        latest_time = max(s.time for s in snaps)
        freshness = format_freshness(latest_time, now)
        stale = is_stale(latest_time, now, config.stale_threshold_hours)
        rows.append((tag, len(snaps), latest_time, freshness, stale))

    tag_w = max(len(r[0]) for r in rows)
    tag_w = max(tag_w, 3)
    snap_w = max(len(str(r[1])) for r in rows)
    snap_w = max(snap_w, 5)

    header = (
        f"{'TAG':<{tag_w}}  {'SNAPS':>{snap_w}}  "
        f"{'LATEST':<19}  FRESHNESS"
    )
    print(header)
    print("-" * len(header))

    for tag, count, latest_time, freshness, stale_flag in rows:
        latest_str = latest_time.strftime("%Y-%m-%d %H:%M:%S")
        stale_marker = " (!)" if stale_flag else ""
        print(
            f"{tag:<{tag_w}}  {count:>{snap_w}}  "
            f"{latest_str:<19}  {freshness}{stale_marker}"
        )


def print_tag_detail(
    snapshots: list[Snapshot], now: datetime, config: BackupConfig,
) -> None:
    sorted_snaps = sorted(snapshots, key=lambda s: s.time, reverse=True)

    rows: list[tuple[str, str, str, str, bool]] = []
    for snap in sorted_snaps:
        time_str = snap.time.strftime("%Y-%m-%d %H:%M:%S")
        freshness = format_freshness(snap.time, now)
        stale = is_stale(snap.time, now, config.stale_threshold_hours)
        paths_str = ", ".join(snap.paths)
        rows.append((snap.short_id, time_str, freshness, paths_str, stale))

    id_w = max(len(r[0]) for r in rows)
    id_w = max(id_w, 2)
    path_w = max(len(r[3]) for r in rows) if rows else 5
    path_w = max(path_w, 5)

    header = f"{'ID':<{id_w}}  {'TIME':<19}  {'FRESHNESS':<14}  PATHS"
    print(header)
    print("-" * len(header))

    for snap_id, time_str, freshness, paths_str, stale_flag in rows:
        stale_marker = " (!)" if stale_flag else ""
        print(
            f"{snap_id:<{id_w}}  {time_str:<19}  "
            f"{freshness + stale_marker:<14}  {paths_str}"
        )
