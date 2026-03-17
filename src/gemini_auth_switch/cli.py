from __future__ import annotations

import argparse
from typing import Sequence

from .paths import GeminiPaths
from .store import GeminiAuthPool, PoolError, ProfileCheckResult, ProfileSummary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gswitch",
        description="Manage multiple local Gemini CLI OAuth accounts",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List saved profiles")
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


def cmd_list(pool: GeminiAuthPool) -> int:
    profiles = pool.list_profiles()
    if not profiles:
        print("no saved profiles")
        return 0

    for summary in profiles:
        marker = "*" if summary.is_current else " "
        email = summary.email or "-"
        print(
            f"{marker} {summary.name:<24} email={email:<30} updated={summary.updated_at}"
        )
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


def cmd_paths(pool: GeminiAuthPool) -> int:
    paths = pool.paths
    print(f"gemini_dir={paths.gemini_dir}")
    print(f"profiles_dir={paths.profiles_dir}")
    print(f"live_creds={paths.live_creds_file}")
    print(f"state_file={paths.state_file}")
    return 0


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    pool = GeminiAuthPool(GeminiPaths.from_home())

    try:
        if args.command == "list":
            return cmd_list(pool)
        if args.command == "current":
            return cmd_current(pool)
        if args.command == "doctor":
            return cmd_doctor(pool)
        if args.command == "check":
            return cmd_check(pool, args)
        if args.command == "check-all":
            return cmd_check_all(pool, args)
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
