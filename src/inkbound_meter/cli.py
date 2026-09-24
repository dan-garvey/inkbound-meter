from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .config import data_directory, default_log
from .service import Session, replay


def parser():
    root = argparse.ArgumentParser(description="Inkbound party damage meter")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command")
    live = commands.add_parser("live", help="Follow the game log (default)")
    live.add_argument("--log", type=Path, default=default_log())
    live.add_argument("--data-dir", type=Path, default=data_directory())
    live.add_argument(
        "--headless", action="store_true", help="Print JSON updates without an overlay"
    )
    live.add_argument("--once", action="store_true", help="Catch up, print JSON and exit")
    replay_command = commands.add_parser(
        "replay", help="Analyze a saved log without changing live data"
    )
    replay_command.add_argument("log", type=Path)
    replay_command.add_argument("--output", type=Path, help="Save a JSON report")
    replay_command.add_argument("--gui", action="store_true", help="Show the last captured run")
    replay_command.add_argument("--data-dir", type=Path, default=data_directory() / "replay")
    demo = commands.add_parser("demo", help="Preview the overlay with clearly labelled sample data")
    demo.add_argument("--data-dir", type=Path, default=data_directory() / "demo")
    for command in (live, replay_command, demo):
        command.add_argument("--screenshot", type=Path, help=argparse.SUPPRESS)
        command.add_argument("--quit-after", type=int, help=argparse.SUPPRESS)
    return root


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list:
        args_list = ["live"]
    args = parser().parse_args(args_list)
    if args.command is None:
        parser().print_help()
        return 0
    try:
        if args.command == "replay":
            meter = replay(args.log)
            report = json.dumps(meter.report(), indent=2, ensure_ascii=False) + "\n"
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(report, encoding="utf-8")
            elif not args.gui:
                print(report, end="")
            if not args.gui:
                return 0
        elif args.command == "demo":
            from .demo import demo_meter

            meter = demo_meter()
        elif args.headless or args.once:
            session = Session(args.log, args.data_dir / "meter.sqlite3")
            try:
                while True:
                    result = session.poll()
                    if result.catching_up:
                        continue
                    if result.events or args.once:
                        print(
                            json.dumps(
                                {"status": session.status, **session.meter.report()},
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                    if args.once:
                        break
                    time.sleep(0.2)
            finally:
                session.close()
            return 0
        else:
            meter = None
        from .overlay import run_overlay

        return run_overlay(
            settings_path=args.data_dir / "settings.json",
            mode=args.command,
            meter=meter,
            log=args.log if args.command == "live" else None,
            database=args.data_dir / "meter.sqlite3" if args.command == "live" else None,
            screenshot=args.screenshot,
            quit_after=args.quit_after,
        )
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        if sys.stderr:
            print(f"Inkbound Meter: {exc}", file=sys.stderr)
        return 1
