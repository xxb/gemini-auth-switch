from __future__ import annotations

import argparse
from typing import Sequence

from .paths import GeminiPaths
from .store import GeminiAuthPool, PoolError, ProfileSummary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gswitch",
        description="Manage multiple local Gemini CLI OAuth accounts",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List saved profiles")
    subparsers.add_parser("current", help="Show the current live profile match")

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
    print(f"live_creds_path={current['live_creds_path']}")
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
        if args.command == "paths":
            return cmd_paths(pool)
        if args.command == "save":
            summary = pool.save_current(args.name, overwrite=args.overwrite)
            print_profile(summary)
            return 0
        if args.command == "use":
            summary = pool.use_profile(args.name)
            print_profile(summary, switched=True)
            print("restart any running Gemini CLI session to apply the new account")
            return 0
        if args.command == "next":
            summary = pool.next_profile()
            print_profile(summary, switched=True)
            print("restart any running Gemini CLI session to apply the new account")
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
