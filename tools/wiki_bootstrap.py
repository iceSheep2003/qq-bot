"""Publish the collected history into LM Wiki Markdown, once.

The Wiki replaces the old slang and memory extraction paths, so this is the
one-off step that turns what has already been collected into Markdown pages the
runtime can read. It reads the **corpus**, not the empty live tables: the group
history that was pulled on 2026-10-13 lives under ``data/private/derived`` as a
pseudonymised JSONL build, and that is the thing with the data in it.

    .venv/bin/python -m tools.wiki_bootstrap --from-corpus
    .venv/bin/python -m tools.wiki_bootstrap --from-corpus --write

It writes nothing unless asked, and it says what it skipped and why — an empty
report with no explanation reads like a bug.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from qqbot.agent.memory.wiki.migration import WikiMigration

DEFAULT_CORPUS = "data/private/derived"


def newest_corpus(root: Path) -> Path | None:
    candidates = sorted(root.glob("behavior-corpus-*"), key=lambda path: path.name)
    return candidates[-1] if candidates else None


def _stamp(iso: str | None) -> str:
    if not iso:
        return "未知"
    try:
        return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%Y-%m-%d")
    except ValueError:
        return iso[:10]


def corpus_rows(corpus: Path) -> tuple[dict[str, list[dict]], list[str]]:
    """One group page per collected group, with facts the corpus can prove."""
    report = json.loads((corpus / "report.json").read_text(encoding="utf-8"))
    speakers: dict[str, Counter] = {}
    messages_path = corpus / "messages.jsonl"
    if messages_path.is_file():
        with messages_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                speakers.setdefault(row.get("group_id", ""), Counter())[row.get("speaker_id", "")] += 1

    memories: list[dict] = []
    for index, group in enumerate(report.get("groups", [])):
        group_id = str(group.get("group_id") or "")
        if not group_id:
            continue
        top = speakers.get(group_id, Counter()).most_common(3)
        parts = [
            f"本群在 {_stamp(group.get('first_time_utc'))} 至 {_stamp(group.get('last_time_utc'))} 之间共收集到 "
            f"{int(group.get('accepted_messages') or 0)} 条消息，来自 {int(group.get('speakers') or 0)} 位发言人。"
        ]
        if top:
            parts.append("发言最多的是 " + "、".join(f"{name}（{count} 条）" for name, count in top) + "。")
        flags = group.get("flags") or {}
        if flags:
            parts.append("标注：" + "、".join(f"{key} {value}" for key, value in sorted(flags.items())) + "。")
        memories.append(
            {
                "id": index + 1,
                "scope": f"group:{group_id}",
                "user_id": "_group_",
                "content": " ".join(parts),
                "fact_type": "fact",
                "created_at": int(group.get("last_time_utc") and _epoch(group["last_time_utc"]) or 0),
                "updated_at": int(group.get("last_time_utc") and _epoch(group["last_time_utc"]) or 0),
                "status": "active",
            }
        )
    notes = []
    if not memories:
        notes.append(f"{corpus.name} 的 report.json 里没有 group 条目")
    return {"memories": memories, "topics": [], "slang": []}, notes


def _epoch(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def legacy_rows(database: Path) -> dict[str, list[dict]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return {
            "memories": [dict(row) for row in connection.execute(
                "SELECT id, scope, user_id, content, fact_type, persona_summary, mention_policy,"
                " source_event_id, created_at, updated_at, status FROM memories")],
            "topics": [dict(row) for row in connection.execute(
                "SELECT id, scope, canonical_name AS name, summary, first_seen_at AS created_at,"
                " last_seen_at AS updated_at FROM memory_topics")],
            "slang": [dict(row) for row in connection.execute(
                "SELECT id, scope, term, meaning, context_meaning, standalone_meaning, confidence,"
                " last_seen, status FROM slang_candidates")],
        }
    finally:
        connection.close()


def diagnose(rows: dict[str, list[dict]]) -> list[str]:
    notes: list[str] = []
    slang = rows["slang"]
    if slang:
        unmeaning = [row for row in slang if not any(
            str(row.get(key) or "").strip()
            for key in ("meaning", "context_meaning", "standalone_meaning")
        )]
        if unmeaning:
            notes.append(
                f"slang_candidates 有 {len(unmeaning)}/{len(slang)} 条没有释义，"
                "Wiki 只收有释义的黑话——这一步要模型推理"
            )
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把已收集的历史投影成 LM Wiki Markdown")
    parser.add_argument("--from-corpus", action="store_true", help="从 data/private/derived 的语料库读取")
    parser.add_argument("--corpus", default=None, help="指定语料目录（默认取最新的 behavior-corpus-*）")
    parser.add_argument("--db", default="data/qunbot.sqlite3")
    parser.add_argument("--root", default="data/wiki")
    parser.add_argument("--write", action="store_true", help="真正写盘；默认只报告")
    args = parser.parse_args(argv)

    notes: list[str] = []
    rows: dict[str, list[dict]] = {"memories": [], "topics": [], "slang": []}

    if args.from_corpus or args.corpus:
        corpus = ROOT / args.corpus if args.corpus else newest_corpus(ROOT / DEFAULT_CORPUS)
        if corpus is None or not corpus.is_dir():
            print("找不到语料库：先跑一次行为语料构建", file=sys.stderr)
            return 2
        print(f"语料库 {corpus.relative_to(ROOT)}")
        rows, corpus_notes = corpus_rows(corpus)
        notes.extend(corpus_notes)

    database = ROOT / args.db
    if database.is_file():
        legacy = legacy_rows(database)
        print(f"在线库 {args.db}: memories {len(legacy['memories'])} · topics {len(legacy['topics'])} · slang {len(legacy['slang'])}")
        for key in ("memories", "topics", "slang"):
            rows[key] = [*rows[key], *legacy[key]]
        notes.extend(diagnose(legacy))

    print(f"可投影 memories {len(rows['memories'])} 行")
    for note in notes:
        print(f"  ! {note}")

    report = WikiMigration().run(rows, ROOT / args.root, write=args.write)
    print(
        f"\n页面 {report.pages} · 事实 {report.entries} · 主题 {report.topics} · "
        f"黑话 {report.slang} · 写入 {report.written}"
        + ("（dry-run，未写盘）" if report.dry_run else "")
    )
    if report.dry_run:
        print("加 --write 才会写入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
