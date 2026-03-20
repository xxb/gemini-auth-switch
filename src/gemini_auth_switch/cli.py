from __future__ import annotations

import argparse
from typing import Sequence

from .paths import GeminiPaths
from .store import (
    AutoSwitchDecision,
    GeminiAuthPool,
    ModelUsageStat,
    PoolError,
    ProfileCheckResult,
    ProfilePickResult,
    ProfileSummary,
    ProfileStatsResult,
    compact_output,
)


def add_model_filter_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--match",
        action="append",
        default=[],
        help="Case-insensitive AND keyword filter for model names; may be supplied multiple times",
    )
    parser.add_argument(
        "--match-any",
        action="append",
        default=[],
        help="Case-insensitive OR keyword filter for model names; may be supplied multiple times",
    )
    parser.add_argument(
        "--exclude-match",
        action="append",
        default=[],
        help="Case-insensitive exclusion keyword filter for model names; may be supplied multiple times",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gswitch",
        description="Manage multiple local Gemini CLI OAuth accounts",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List saved profiles")
    list_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show extra fields such as saved-at time and probe detail",
    )
    subparsers.add_parser("current", help="Show the current live profile match")
    subparsers.add_parser("doctor", help="Show auth diagnostics and common failure hints")
    check_one_parser = subparsers.add_parser(
        "check",
        help="Probe one saved profile with a fresh Gemini subprocess",
    )
    check_one_parser.add_argument("name", help="Saved profile name to probe")
    check_one_parser.add_argument(
        "--gemini-bin", default="gemini", help="Gemini executable to launch"
    )
    check_one_parser.add_argument(
        "--gemini-arg",
        action="append",
        default=[],
        help="Extra argument passed to Gemini; may be supplied multiple times",
    )
    check_one_parser.add_argument(
        "--prompt",
        default="ping",
        help="Prompt sent with `gemini -p` during the probe",
    )
    check_one_parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Probe timeout in seconds",
    )
    check_parser = subparsers.add_parser(
        "check-all",
        help="Probe all saved profiles with a fresh Gemini subprocess",
    )
    check_parser.add_argument("--gemini-bin", default="gemini", help="Gemini executable to launch")
    check_parser.add_argument(
        "--gemini-arg",
        action="append",
        default=[],
        help="Extra argument passed to Gemini; may be supplied multiple times",
    )
    check_parser.add_argument(
        "--prompt",
        default="ping",
        help="Prompt sent with `gemini -p` during each probe",
    )
    check_parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Per-profile probe timeout in seconds",
    )
    check_parser.add_argument(
        "--delay",
        type=float,
        default=5.0,
        help="Sleep between profiles in seconds to reduce rapid probing",
    )
    check_parser.add_argument(
        "--limit",
        type=int,
        help="Only probe the first N saved profiles",
    )
    stats_one_parser = subparsers.add_parser(
        "stats",
        help="Collect Gemini /stats output for one saved profile",
    )
    stats_one_parser.add_argument("name", help="Saved profile name to inspect")
    stats_one_parser.add_argument(
        "--gemini-bin", default="gemini", help="Gemini executable to launch"
    )
    stats_one_parser.add_argument(
        "--gemini-arg",
        action="append",
        default=[],
        help="Extra argument passed to Gemini; may be supplied multiple times",
    )
    stats_one_parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Stats collection timeout in seconds",
    )
    stats_parser = subparsers.add_parser(
        "stats-all",
        help="Collect Gemini /stats output for all saved profiles",
    )
    stats_parser.add_argument("--gemini-bin", default="gemini", help="Gemini executable to launch")
    stats_parser.add_argument(
        "--gemini-arg",
        action="append",
        default=[],
        help="Extra argument passed to Gemini; may be supplied multiple times",
    )
    stats_parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Per-profile stats timeout in seconds",
    )
    stats_parser.add_argument(
        "--delay",
        type=float,
        default=5.0,
        help="Sleep between profiles in seconds to reduce rapid quota polling",
    )
    stats_parser.add_argument(
        "--limit",
        type=int,
        help="Only collect stats for the first N saved profiles",
    )
    quota_parser = subparsers.add_parser(
        "quota",
        help="Show cached quota data for one saved profile",
    )
    quota_parser.add_argument(
        "name",
        nargs="?",
        help="Saved profile name to inspect; defaults to the current profile",
    )
    quota_parser.add_argument(
        "--summary",
        action="store_true",
        help="Show only the cached summary line",
    )
    quota_all_parser = subparsers.add_parser(
        "quota-all",
        help="Show cached quota data for all saved profiles",
    )
    quota_all_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include per-model cached quota rows",
    )
    pick_parser = subparsers.add_parser(
        "pick",
        help="Choose the best saved profile from cached quota data",
    )
    add_model_filter_arguments(pick_parser)
    auto_use_parser = subparsers.add_parser(
        "auto-use",
        help="Automatically keep or switch profiles using cached quota plus on-demand refresh",
    )
    add_model_filter_arguments(auto_use_parser)
    auto_use_parser.add_argument(
        "--min-remaining",
        type=float,
        default=15.0,
        help="Keep the current profile when its matched quota after refresh is at or above this percentage",
    )
    auto_use_parser.add_argument(
        "--stale-seconds",
        type=float,
        default=300.0,
        help="Refresh a quota cache entry when it is older than this many seconds",
    )
    auto_use_parser.add_argument(
        "--candidate-refresh-limit",
        type=int,
        default=2,
        help="Maximum stale or missing non-current candidates to refresh before deciding",
    )
    auto_use_parser.add_argument(
        "--avoid-profile",
        action="append",
        default=[],
        help="Temporarily exclude a saved profile from this decision; may be supplied multiple times",
    )
    auto_use_parser.add_argument(
        "--gemini-bin",
        default="gemini",
        help="Gemini executable used to locate the official CLI core package",
    )

    mark_rate_limited_parser = subparsers.add_parser(
        "mark-rate-limited",
        help=argparse.SUPPRESS,
    )
    mark_rate_limited_parser.add_argument("name")
    mark_rate_limited_parser.add_argument(
        "--detail",
        default="Gemini request hit a rate limit.",
    )
    mark_rate_limited_parser.add_argument(
        "--retry-after",
        type=float,
        default=None,
    )

    save_parser = subparsers.add_parser("save", help="Save the current live account")
    save_parser.add_argument("name", nargs="?", help="Profile name; defaults to live email if available")
    save_parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing profile")

    use_parser = subparsers.add_parser("use", help="Switch to a saved profile")
    use_parser.add_argument("name")

    remove_parser = subparsers.add_parser("remove", help="Delete a saved profile")
    remove_parser.add_argument("name")

    subparsers.add_parser("next", help="Rotate to the next saved profile")
    subparsers.add_parser("paths", help="Show important filesystem paths")

    login_parser = subparsers.add_parser("login", help="Launch Gemini login and save the result")
    login_parser.add_argument("name", nargs="?", help="Profile name; defaults to the live email after login")
    login_parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing profile")
    login_parser.add_argument("--gemini-bin", default="gemini", help="Gemini executable to launch")
    login_parser.add_argument(
        "--gemini-arg",
        action="append",
        default=[],
        help="Extra argument passed to Gemini; may be supplied multiple times",
    )

    return parser


def print_profile(summary: ProfileSummary, switched: bool = False) -> None:
    prefix = "switched" if switched else "saved"
    email_part = f" email={summary.email}" if summary.email else ""
    print(f"{prefix} profile={summary.name}{email_part}")


def print_post_switch_notes() -> None:
    print("restart any running Gemini CLI session to apply the new account")
    print(
        "if a fresh Gemini launch asks to verify your account or change login, "
        "that is usually a Google-side account validation issue for that profile"
    )


def print_profile_summaries(profiles: list[ProfileSummary], verbose: bool = False) -> None:
    name_width = max(len(summary.name) for summary in profiles)
    email_width = max(len(summary.email or "-") for summary in profiles)
    for summary in profiles:
        marker = "*" if summary.is_current else " "
        email = summary.email or "-"
        status = summary.last_check_status or "-"
        checked = summary.last_checked_at or "-"
        line = (
            f"{marker} {summary.name:<{name_width}} "
            f"email={email:<{email_width}} "
            f"status={status:<20} "
            f"checked={checked}"
        )
        if verbose:
            detail = compact_output(summary.last_check_detail or "-", limit=96)
            line = f"{line} updated={summary.updated_at or '-'} detail={detail}"
        print(line)


def print_check_summary(results: list[ProfileCheckResult]) -> None:
    name_width = max(len(result.name) for result in results)
    email_width = max(len(result.email or "-") for result in results)
    status_width = max(len(result.status) for result in results)
    for result in results:
        email = result.email or "-"
        detail = compact_output(result.detail, limit=96)
        print(
            f"  {result.name:<{name_width}} "
            f"email={email:<{email_width}} "
            f"status={result.status:<{status_width}} "
            f"checked={result.checked_at} "
            f"detail={detail}"
        )


def print_model_usage(item: ModelUsageStat) -> None:
    requests = item.requests
    remaining = item.usage_remaining
    reset_at_part = f" reset_at={item.reset_at}" if item.reset_at else ""
    print(f"  model={item.model} requests={requests} remaining={remaining}{reset_at_part}")


def format_lowest_remaining(result: ProfileStatsResult) -> str:
    lowest = result.lowest_remaining_percent()
    if lowest is None:
        return "-"
    return f"{lowest:.1f}%"


def print_stats_result(result: ProfileStatsResult) -> None:
    email_part = f" email={result.email}" if result.email else ""
    auth_part = f" auth={result.auth_method}" if result.auth_method else ""
    tier_part = f" tier={result.tier}" if result.tier else ""
    label_part = f" usage={result.usage_label}" if result.usage_label else ""
    print(
        f"{result.status} profile={result.name}{email_part} checked={result.checked_at}"
        f"{label_part} detail={result.detail}{auth_part}{tier_part}"
    )
    for item in result.models:
        print_model_usage(item)


def print_cached_quota_result(result: ProfileStatsResult, summary_only: bool = False) -> None:
    email_part = f" email={result.email}" if result.email else ""
    usage_part = f" usage={result.usage_label}" if result.usage_label else ""
    refresh_part = ""
    if result.last_refresh_status and result.last_refresh_status != "ok":
        refresh_part = f" refresh={result.last_refresh_status}"
        if result.blocked_until:
            refresh_part = f"{refresh_part} blocked_until={result.blocked_until}"
    print(
        f"quota={result.status} profile={result.name}{email_part} checked={result.checked_at}"
        f" lowest={format_lowest_remaining(result)} models={result.model_count()}{usage_part}"
        f" detail={result.detail}{refresh_part}"
    )
    if summary_only:
        return
    for item in result.models:
        print_model_usage(item)


def print_cached_quota_summary(
    results: list[ProfileStatsResult],
    current_profile: str | None,
    verbose: bool = False,
) -> None:
    name_width = max(len(result.name) for result in results)
    email_width = max(len(result.email or "-") for result in results)
    status_width = max(len(result.status) for result in results)
    for result in results:
        marker = "*" if result.name == current_profile else " "
        email = result.email or "-"
        line = (
            f"{marker} {result.name:<{name_width}} "
            f"email={email:<{email_width}} "
            f"quota={result.status:<{status_width}} "
            f"checked={result.checked_at} "
            f"lowest={format_lowest_remaining(result):<6} "
            f"models={result.model_count()}"
        )
        if verbose:
            usage = result.usage_label or "-"
            detail = compact_output(result.detail, limit=96)
            line = f"{line} usage={usage} detail={detail}"
            if result.last_refresh_status and result.last_refresh_status != "ok":
                line = f"{line} refresh={result.last_refresh_status}"
                if result.blocked_until:
                    line = f"{line} blocked_until={result.blocked_until}"
        print(line)
        if verbose:
            for item in result.models:
                print_model_usage(item)


def print_pick_result(result: ProfilePickResult) -> None:
    marker = "*" if result.is_current else " "
    email_part = f" email={result.email}" if result.email else ""
    tier_part = f" tier={result.tier}" if result.tier else ""
    usage_part = f" usage={result.usage_label}" if result.usage_label else ""
    health_part = f" health={result.health_status or '-'}"
    print(
        f"{marker} picked profile={result.name}{email_part} model={result.matched_model} "
        f"remaining={result.usage_remaining} quota_checked={result.quota_checked_at}"
        f"{health_part}{usage_part}{tier_part}"
    )
    print(render_model_filters(result))


def print_auto_switch_decision(decision: AutoSwitchDecision) -> None:
    result = decision.selected
    email_part = f" email={result.email}" if result.email else ""
    tier_part = f" tier={result.tier}" if result.tier else ""
    usage_part = f" usage={result.usage_label}" if result.usage_label else ""
    if decision.action == "switch":
        previous = decision.current_profile or "-"
        print(
            f"switched from={previous} to={result.name}{email_part} model={result.matched_model} "
            f"remaining={result.usage_remaining} threshold={decision.threshold_percent:.1f}% "
            f"quota_checked={result.quota_checked_at} health={result.health_status or '-'}"
            f"{usage_part}{tier_part} reason={decision.reason}"
        )
        print(render_model_filters(result))
        print_post_switch_notes()
        return

    print(
        f"kept profile={result.name}{email_part} model={result.matched_model} "
        f"remaining={result.usage_remaining} threshold={decision.threshold_percent:.1f}% "
        f"quota_checked={result.quota_checked_at} health={result.health_status or '-'}"
        f"{usage_part}{tier_part} reason={decision.reason}"
    )
    print(render_model_filters(result))


def render_model_filters(result: ProfilePickResult) -> str:
    match = ",".join(result.match_terms) if result.match_terms else "-"
    match_any = ",".join(result.match_any_terms) if result.match_any_terms else "-"
    exclude = ",".join(result.exclude_match_terms) if result.exclude_match_terms else "-"
    return f"filters match={match} match_any={match_any} exclude={exclude}"


def print_stats_progress(event: str, payload: dict) -> None:
    if event == "start":
        email_part = f" email={payload['email']}" if payload.get("email") else ""
        print(
            f"collecting {payload['index']}/{payload['total']} "
            f"profile={payload['name']}{email_part}",
            flush=True,
        )
        return
    if event == "result":
        result: ProfileStatsResult = payload["result"]
        print(
            f"{result.status} profile={result.name} checked={result.checked_at} detail={result.detail}",
            flush=True,
        )
        return
    if event == "delay":
        print(
            f"waiting seconds={payload['seconds']:g} before profile "
            f"{payload['next_index']}/{payload['total']}",
            flush=True,
        )


def cmd_list(pool: GeminiAuthPool, args: argparse.Namespace) -> int:
    profiles = pool.list_profiles()
    if not profiles:
        print("no saved profiles")
        return 0

    print_profile_summaries(profiles, verbose=args.verbose)
    return 0


def cmd_current(pool: GeminiAuthPool) -> int:
    current = pool.current_summary()
    print(f"profile={current['profile'] or '-'}")
    print(f"email={current['email'] or '-'}")
    print(f"has_live_creds={str(current['has_live_creds']).lower()}")
    print(f"selected_auth_type={current['selected_auth_type'] or '-'}")
    print(f"live_creds_path={current['live_creds_path']}")
    return 0


def cmd_doctor(pool: GeminiAuthPool) -> int:
    summary = pool.diagnostics_summary()
    print(f"profile={summary['profile'] or '-'}")
    print(f"email={summary['email'] or '-'}")
    print(f"has_live_creds={str(summary['has_live_creds']).lower()}")
    print(f"selected_auth_type={summary['selected_auth_type'] or '-'}")
    print(f"token_cache_v1_exists={str(summary['token_cache_v1_exists']).lower()}")
    print(f"token_cache_v2_exists={str(summary['token_cache_v2_exists']).lower()}")
    print(f"force_file_storage={str(summary['force_file_storage']).lower()}")
    print(
        "force_encrypted_file_storage="
        f"{str(summary['force_encrypted_file_storage']).lower()}"
    )
    print("note=restart any running Gemini CLI session after switching accounts")
    if summary["selected_auth_type"] not in {None, "oauth-personal"}:
        print(
            "warning=selected auth type is not oauth-personal; "
            "gswitch only manages Gemini OAuth personal accounts"
        )
    if summary["force_encrypted_file_storage"]:
        print(
            "warning=GEMINI_FORCE_ENCRYPTED_FILE_STORAGE=true makes Gemini prefer "
            "encrypted storage instead of oauth_creds.json"
        )
    print(
        "note=if a fresh Gemini launch asks to verify your account or change login, "
        "that is usually Google-side account validation rather than a failed local switch"
    )
    return 0


def print_check_result(result: ProfileCheckResult) -> None:
    email_part = f" email={result.email}" if result.email else ""
    returncode_part = (
        f" returncode={result.returncode}" if result.returncode is not None else ""
    )
    print(
        f"{result.status} profile={result.name}{email_part}"
        f"{returncode_part} detail={result.detail}"
    )


def print_check_progress(event: str, payload: dict) -> None:
    if event == "start":
        email_part = f" email={payload['email']}" if payload.get("email") else ""
        print(
            f"checking {payload['index']}/{payload['total']} "
            f"profile={payload['name']}{email_part}",
            flush=True,
        )
        return
    if event == "result":
        print_check_result(payload["result"])
        return
    if event == "delay":
        print(
            f"waiting seconds={payload['seconds']:g} before profile "
            f"{payload['next_index']}/{payload['total']}",
            flush=True,
        )


def cmd_check_all(pool: GeminiAuthPool, args: argparse.Namespace) -> int:
    print("note=check-all reuses saved local credentials; it does not reopen browser login")
    print(
        "note=each probe still starts a fresh Gemini process and API request; "
        "use --delay to reduce rapid multi-account probing"
    )
    results = pool.check_all_profiles(
        gemini_bin=args.gemini_bin,
        prompt=args.prompt,
        timeout_seconds=args.timeout,
        delay_seconds=args.delay,
        limit=args.limit,
        gemini_args=args.gemini_arg,
        progress_callback=print_check_progress,
    )

    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    summary = " ".join(f"{name}={counts[name]}" for name in sorted(counts))
    print(f"summary total={len(results)} {summary}")
    print("results:")
    print_check_summary(results)
    print("note=original live auth was restored after the probe run")
    return 0


def cmd_check(pool: GeminiAuthPool, args: argparse.Namespace) -> int:
    print("note=check reuses saved local credentials; it does not reopen browser login")
    result = pool.check_profile(
        args.name,
        gemini_bin=args.gemini_bin,
        prompt=args.prompt,
        timeout_seconds=args.timeout,
        gemini_args=args.gemini_arg,
    )
    print_check_result(result)
    print("note=original live auth was restored after the probe run")
    return 0


def cmd_stats(pool: GeminiAuthPool, args: argparse.Namespace) -> int:
    print("note=stats reuses saved local credentials; it does not reopen browser login")
    print(
        "note=stats starts a fresh Gemini TTY session and runs /stats for the selected profile"
    )
    result = pool.stats_profile(
        args.name,
        gemini_bin=args.gemini_bin,
        timeout_seconds=args.timeout,
        gemini_args=args.gemini_arg,
    )
    print_stats_result(result)
    print("note=latest quota snapshot was cached locally")
    print("note=original live auth was restored after the stats run")
    return 0


def cmd_stats_all(pool: GeminiAuthPool, args: argparse.Namespace) -> int:
    print("note=stats-all reuses saved local credentials; it does not reopen browser login")
    print(
        "note=each stats run starts a fresh Gemini TTY session and executes /stats; "
        "use --delay to reduce rapid multi-account polling"
    )
    results = pool.stats_all_profiles(
        gemini_bin=args.gemini_bin,
        timeout_seconds=args.timeout,
        delay_seconds=args.delay,
        limit=args.limit,
        gemini_args=args.gemini_arg,
        progress_callback=print_stats_progress,
    )
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    summary = " ".join(f"{name}={counts[name]}" for name in sorted(counts))
    print(f"summary total={len(results)} {summary}")
    print("results:")
    for result in results:
        print_stats_result(result)
    print("note=latest quota snapshots were cached locally")
    print("note=original live auth was restored after the stats run")
    return 0


def cmd_quota(pool: GeminiAuthPool, args: argparse.Namespace) -> int:
    result = pool.quota_profile(args.name)
    print_cached_quota_result(result, summary_only=args.summary)
    return 0


def cmd_quota_all(pool: GeminiAuthPool, args: argparse.Namespace) -> int:
    results = pool.quota_all_profiles()
    print_cached_quota_summary(results, pool.current_profile_name(), verbose=args.verbose)
    return 0


def cmd_pick(pool: GeminiAuthPool, args: argparse.Namespace) -> int:
    result = pool.pick_profile(
        args.match,
        args.match_any,
        args.exclude_match,
    )
    print_pick_result(result)
    return 0


def cmd_auto_use(pool: GeminiAuthPool, args: argparse.Namespace) -> int:
    decision = pool.auto_use_profile(
        match_terms=args.match,
        match_any_terms=args.match_any,
        exclude_terms=args.exclude_match,
        min_remaining_percent=args.min_remaining,
        stale_seconds=args.stale_seconds,
        candidate_refresh_limit=args.candidate_refresh_limit,
        gemini_bin=args.gemini_bin,
        avoid_profiles=args.avoid_profile,
    )
    print_auto_switch_decision(decision)
    return 0


def cmd_mark_rate_limited(pool: GeminiAuthPool, args: argparse.Namespace) -> int:
    result = pool.mark_profile_rate_limited(
        args.name,
        detail=args.detail,
        retry_after_seconds=args.retry_after,
    )
    blocked_part = f" blocked_until={result.blocked_until}" if result.blocked_until else ""
    print(f"marked profile={result.name} quota={result.status}{blocked_part} detail={result.detail}")
    return 0


def cmd_paths(pool: GeminiAuthPool) -> int:
    paths = pool.paths
    print(f"gemini_dir={paths.gemini_dir}")
    print(f"profiles_dir={paths.profiles_dir}")
    print(f"live_creds={paths.live_creds_file}")
    print(f"state_file={paths.state_file}")
    print(f"check_state_file={paths.check_state_file}")
    print(f"quota_state_file={paths.quota_state_file}")
    return 0


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    pool = GeminiAuthPool(GeminiPaths.from_home())

    try:
        if args.command == "list":
            return cmd_list(pool, args)
        if args.command == "current":
            return cmd_current(pool)
        if args.command == "doctor":
            return cmd_doctor(pool)
        if args.command == "check":
            return cmd_check(pool, args)
        if args.command == "check-all":
            return cmd_check_all(pool, args)
        if args.command == "stats":
            return cmd_stats(pool, args)
        if args.command == "stats-all":
            return cmd_stats_all(pool, args)
        if args.command == "quota":
            return cmd_quota(pool, args)
        if args.command == "quota-all":
            return cmd_quota_all(pool, args)
        if args.command == "pick":
            return cmd_pick(pool, args)
        if args.command == "auto-use":
            return cmd_auto_use(pool, args)
        if args.command == "mark-rate-limited":
            return cmd_mark_rate_limited(pool, args)
        if args.command == "paths":
            return cmd_paths(pool)
        if args.command == "save":
            summary = pool.save_current(args.name, overwrite=args.overwrite)
            print_profile(summary)
            return 0
        if args.command == "use":
            summary = pool.use_profile(args.name)
            print_profile(summary, switched=True)
            print_post_switch_notes()
            return 0
        if args.command == "next":
            summary = pool.next_profile()
            print_profile(summary, switched=True)
            print_post_switch_notes()
            return 0
        if args.command == "remove":
            pool.remove_profile(args.name)
            print(f"removed profile={args.name}")
            return 0
        if args.command == "login":
            if not args.gemini_arg:
                print("launching Gemini login flow; complete authentication, then exit Gemini")
            summary = pool.login(
                args.name,
                gemini_bin=args.gemini_bin,
                gemini_args=args.gemini_arg,
                overwrite=args.overwrite,
            )
            print_profile(summary)
            print_post_switch_notes()
            return 0
    except PoolError as exc:
        print(f"error: {exc}")
        return 2

    parser.error(f"unknown command: {args.command}")
    return 2


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
