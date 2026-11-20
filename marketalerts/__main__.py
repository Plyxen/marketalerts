"""Command line: python -m marketalerts <command>"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    sys.exit("marketalerts needs Python 3.11 or newer.")

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

from . import notify, sources
from .config import DEFAULT_CONFIG, ROOT, WEBHOOK_ENV, load_config
from .core import State, check_once, preview, sample_cards
from .sources import clean


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="marketalerts")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-dir", type=Path, default=ROOT / "state")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="check every source once")
    check.add_argument("--dry-run", action="store_true", help="print instead of sending; saves nothing")
    commands.add_parser("loop", help="check again and again, forever")
    look = commands.add_parser("preview", help="show what recent days would have sent; sends nothing")
    look.add_argument("--days", type=float, default=10.0)
    commands.add_parser("test", help="send one test message to Discord")
    commands.add_parser("sample", help="send one real example of each kind of card to Discord")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"No config at {args.config}. Copy config.example.toml to config.toml first.",
              file=sys.stderr)
        return 2
    except (ValueError, TypeError) as error:
        print(f"Config problem: {error}", file=sys.stderr)
        return 2

    state_dir: Path = args.state_dir
    needs_webhook = args.command in ("loop", "test", "sample") or (
        args.command == "check" and not args.dry_run
    )
    if needs_webhook and not config.webhook_url:
        print(f"No Discord webhook. Set discord.webhook_url in the config or {WEBHOOK_ENV}.",
              file=sys.stderr)
        return 2

    try:
        if args.command == "preview":
            preview(config, cache_dir=state_dir, days=args.days)
        elif args.command == "test":
            watching = "\n".join(f"• {clean(w.name)} ({clean(w.symbol, 20)})" for w in config.watches)
            card = sources.card(
                label="Market alerts", title="✅ Connected", colour=sources.GREEN,
                when=datetime.now(timezone.utc), footer="marketalerts",
                description="Alerts for filings, news and price drops will appear here.",
                fields=[("Watching", watching)],
            )
            notify.send(config.webhook_url, [[card]], username=config.username)
            print("Test message sent.")
        elif args.command == "sample":
            cards = sample_cards(config, cache_dir=state_dir)
            notify.send(config.webhook_url, notify.pack(cards), username=config.username)
            print(f"{len(cards)} sample card(s) sent.")
        elif args.command == "check":
            state = State(state_dir / "state.json")
            sent = check_once(config, state, cache_dir=state_dir, dry_run=args.dry_run)
            print(f"{sent} alert(s){' (dry run)' if args.dry_run else ' sent'}.")
        else:
            return run_forever(config, state_dir)
    except notify.DeliveryError as error:
        print(f"Not delivered: {error}. It will be tried again on the next check.",
              file=sys.stderr)
        return 1
    return 0


def run_forever(config, state_dir: Path) -> int:
    print(f"Checking every {config.settings.check_minutes:g} minutes. Ctrl+C stops it.")
    while True:
        try:
            state = State(state_dir / "state.json")
            sent = check_once(config, state, cache_dir=state_dir)
            if sent:
                print(f"{time.strftime('%Y-%m-%d %H:%M')} sent {sent} alert(s)")
        except notify.DeliveryError as error:
            print(f"Not delivered: {error}. Trying again next check.", file=sys.stderr)
        except Exception as error:  # keep the loop alive whatever one check does
            print(f"Check failed: {type(error).__name__}: {error}", file=sys.stderr)
        try:
            time.sleep(config.settings.check_minutes * 60)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
