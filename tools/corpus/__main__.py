"""Offline corpus commands; run from the repository root."""

import argparse
from pathlib import Path

from tools.corpus.qce import QCEClient, collect
from tools.corpus.build import build
import json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("collect", help="Export only explicitly authorized groups via local QCE")
    export.add_argument("--root", type=Path, required=True)
    export.add_argument("--group", action="append", required=True)
    export.add_argument("--base-url", default="http://127.0.0.1:40653")
    export.add_argument("--security-file", type=Path, default=Path.home() / ".qq-chat-exporter/security.json")
    prepare = commands.add_parser("build", help="Normalize, pseudonymize and split completed local exports")
    prepare.add_argument("--root", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--idle-seconds", type=int, default=1200)
    prepare.add_argument("--context-messages", type=int, default=40)
    prepare.add_argument("--bot-uin", action="append", default=[])
    prepare.add_argument(
        "--completed-only",
        action="store_true",
        help="Process only verified completed exports; report incomplete groups as skipped",
    )
    args = parser.parse_args()
    if args.command == "collect":
        collect(QCEClient(args.base_url, args.security_file), args.root, args.group)
    else:
        report = build(
            args.root,
            args.output,
            args.idle_seconds,
            args.bot_uin,
            args.context_messages,
            args.completed_only,
        )
        print(json.dumps({k:report[k] for k in ("messages", "sessions", "replay_cases", "training_candidates", "quarantined_records", "duplicate_records", "split")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
