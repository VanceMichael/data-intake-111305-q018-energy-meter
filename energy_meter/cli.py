"""结构化接入与财务查询的命令行入口（只读查询不做任何计费动作）。"""
from __future__ import annotations

import json
from pathlib import Path

from .archive import Archive
from .client import Client, collect
from .decoding import records as decode_records
from .finance import (
    finance_comparison,
    meter_series,
    pending_impacts,
    trace_reading,
    trace_suggestion,
)
from .ledger import Ledger
from .origin import load_scenario, serve


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def publish_rules(args) -> dict:
    ledger = Ledger(args.ledger)
    try:
        book = ledger.publish_rules(_load_json(args.rules))
        return {"rule_basis_id": book.id, "digest": book.digest,
                "published_at": book.published_at,
                "meter_intervals": len(book.meter_intervals())}
    finally:
        ledger.close()


def ingest_feed(args) -> dict:
    ledger = Ledger(args.ledger)
    archive = None
    try:
        if args.archive is not None:
            archive = Archive(args.archive)
            response = collect(Client(args.base_url), archive, args.source, args.path)[1]
            if response.status != 200:
                raise SystemExit("source responded with status " + str(response.status))
            items = decode_records(response.body, response.headers.get("content-type", ""))
            capture_id = archive.captures(args.source)[-1]["id"]
        else:
            items = _load_json(args.file)
            if isinstance(items, dict):
                items = items.get("items", [])
            capture_id = None
        result = ledger.ingest_records(
            items, capture_id=capture_id, source=args.source,
            rule_book=ledger.latest_rulebook())
        result["messages"] = len(ledger.interpretations())
        return result
    finally:
        ledger.close()
        if archive is not None:
            archive.close()


def issue_snapshot(args) -> dict:
    ledger = Ledger(args.ledger)
    try:
        return ledger.issue_snapshot(args.period, note=args.note)
    finally:
        ledger.close()


def adjust(args) -> dict:
    ledger = Ledger(args.ledger)
    try:
        if args.accept is not None:
            return ledger.accept_adjustment(args.accept, note=args.note)
        return ledger.generate_adjustments(args.snapshot_id)
    finally:
        ledger.close()


def query_finance(args) -> dict:
    ledger = Ledger(args.ledger)
    try:
        if args.command == "comparison":
            return finance_comparison(ledger, args.snapshot_id,
                                      include_unissued=not args.issued_only)
        if args.command == "pending":
            return pending_impacts(ledger, args.period)
        if args.command == "series":
            return meter_series(ledger, args.provider, args.meter)
        if args.command == "trace":
            archive = Archive(args.archive) if args.archive else None
            try:
                if args.suggestion_id is not None:
                    return trace_suggestion(ledger, args.suggestion_id, archive=archive)
                return trace_reading(ledger, args.reading_key, archive=archive)
            finally:
                if archive is not None:
                    archive.close()
        raise AssertionError("unknown finance command")
    finally:
        ledger.close()


def run_demo() -> dict:
    """空目录上的完整生命周期：乱序补传、签发、更正、接受、再更正、查询。"""
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="energy-meter-demo-"))
    archive_path = root / "raw.sqlite3"
    ledger_path = root / "ledger.sqlite3"
    rules_path = Path(__file__).parent / "fixtures" / "ledger-rules.json"
    if not rules_path.is_file():
        rules_path = Path(__file__).parents[1] / "fixtures" / "ledger-rules.json"
    scenario_path = Path(__file__).parent / "fixtures" / "staged-feed.json"
    if not scenario_path.is_file():
        scenario_path = Path(__file__).parents[1] / "fixtures" / "staged-feed.json"

    archive = Archive(archive_path)
    ledger = Ledger(ledger_path)
    summary = {"workdir": str(root)}
    try:
        with serve(load_scenario(scenario_path)) as origin:
            client = Client(origin.base_url)
            book = ledger.publish_rules(_load_json(rules_path))
            summary["rule_basis"] = book.id

            def pull(path):
                capture_id, response = collect(client, archive, "local-source", path)
                items = decode_records(response.body, response.headers["content-type"])
                return ledger.ingest_records(items, capture_id=capture_id,
                                             source="local-source")

            # 阶段一：估算读数先到，按它签发十月月账。
            summary["stage_1_estimated"] = pull("/feed/stage-1")
            snapshot = ledger.issue_snapshot("2026-10", note="demo 十月月账")
            summary["issued_snapshot"] = snapshot
            # 阶段二：实测修订2乱序后到；同帧重放一次验证幂等。
            summary["stage_2_measured"] = pull("/feed/stage-2")
            summary["stage_2_redelivery"] = pull("/feed/stage-2")
            summary["first_adjustment"] = ledger.generate_adjustments(
                snapshot["snapshot_id"])
            first_id = summary["first_adjustment"]["created"][0]["suggestion_id"]
            summary["finance_accepts"] = ledger.accept_adjustment(first_id, note="财务接受")
            # 阶段三：换表后新表首数，倍率未知 → 待核。
            summary["stage_3_new_meter_pending_factor"] = pull("/feed/stage-3")
            # 阶段四：财务接受之后又来两次更正，验证链式差额与去重。
            summary["correction_rev3"] = pull("/feed/correction-1")
            summary["correction_rev4"] = pull("/feed/correction-2")
            generated = ledger.generate_adjustments(snapshot["snapshot_id"])
            summary["follow_up_adjustment"] = generated
            summary["repeat_generation_is_deduplicated"] = ledger.generate_adjustments(
                snapshot["snapshot_id"])
            summary["finance_view"] = finance_comparison(
                ledger, snapshot["snapshot_id"])
            summary["pending_impacts"] = pending_impacts(ledger)
            summary["meter_series_M7"] = {
                "old": meter_series(ledger, "grid-a", "M7-old"),
                "new": meter_series(ledger, "grid-a", "M7-new"),
            }
            summary["trace_R10"] = trace_reading(ledger, "R10", archive=archive)
        return summary
    finally:
        ledger.close()
        archive.close()


def build_parser(subcommands):
    rules_cmd = subcommands.add_parser("publish-rules", help="发布一版适用规则")
    rules_cmd.add_argument("--ledger", type=Path, required=True)
    rules_cmd.add_argument("--rules", type=Path, required=True)
    rules_cmd.set_defaults(handler=publish_rules)

    ingest_cmd = subcommands.add_parser("ingest", help="结构化接入供方读数")
    ingest_cmd.add_argument("--ledger", type=Path, required=True)
    feed = ingest_cmd.add_mutually_exclusive_group(required=True)
    feed.add_argument("--base-url")
    feed.add_argument("--file", type=Path)
    ingest_cmd.add_argument("--archive", type=Path)
    ingest_cmd.add_argument("--source", default="local-source")
    ingest_cmd.add_argument("--path", default="/feed")
    ingest_cmd.set_defaults(handler=ingest_feed)

    snapshot_cmd = subcommands.add_parser("issue-snapshot", help="签发不可变账期快照")
    snapshot_cmd.add_argument("--ledger", type=Path, required=True)
    snapshot_cmd.add_argument("--period", required=True)
    snapshot_cmd.add_argument("--note", default="")
    snapshot_cmd.set_defaults(handler=issue_snapshot)

    adjust_cmd = subcommands.add_parser("adjust", help="生成或接受调整建议")
    adjust_cmd.add_argument("--ledger", type=Path, required=True)
    group = adjust_cmd.add_mutually_exclusive_group(required=True)
    group.add_argument("--snapshot-id", type=int)
    group.add_argument("--accept", type=int, metavar="SUGGESTION_ID")
    adjust_cmd.add_argument("--note", default="")
    adjust_cmd.set_defaults(handler=adjust)

    finance = subcommands.add_parser("finance", help="财务只读查询")
    finance_sub = finance.add_subparsers(dest="command", required=True)

    comparison = finance_sub.add_parser("comparison", help="已签发/当前/待核并列视图")
    comparison.add_argument("--ledger", type=Path, required=True)
    comparison.add_argument("--snapshot-id", type=int, required=True)
    comparison.add_argument("--issued-only", action="store_true")

    pending = finance_sub.add_parser("pending", help="待核影响清单")
    pending.add_argument("--ledger", type=Path, required=True)
    pending.add_argument("--period")

    series = finance_sub.add_parser("series", help="按安装身份查看累计曲线与换表边界")
    series.add_argument("--ledger", type=Path, required=True)
    series.add_argument("--provider", required=True)
    series.add_argument("--meter", required=True)

    trace = finance_sub.add_parser("trace", help="从读数或差异回到报文与规则")
    trace.add_argument("--ledger", type=Path, required=True)
    target = trace.add_mutually_exclusive_group(required=True)
    target.add_argument("--reading-key")
    target.add_argument("--suggestion-id", type=int)
    trace.add_argument("--archive", type=Path)

    for sub in (comparison, pending, series, trace):
        sub.set_defaults(handler=query_finance)
    return subcommands
