import argparse
import json
import tempfile
from pathlib import Path

from .archive import Archive
from .client import Client, collect
from .decoding import records
from .intake import ingest_all, ingest_latest
from .origin import serve
from .store import IngestStore


def capture(base_url, archive_path, source, path):
    archive = Archive(archive_path)
    try:
        capture_id, response = collect(Client(base_url), archive, source, path)
        result = {"capture_id": capture_id, "status": response.status, "bytes": len(response.body), "url": response.url}
        if response.status == 200:
            result["source_records"] = records(response.body, response.headers.get("content-type", ""))
        result["archived_observations"] = len(archive.captures())
        return result
    finally:
        archive.close()


def _open_store(path: Path) -> IngestStore:
    return IngestStore(path)


def _print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def run_ingest(args):
    archive = Archive(args.archive)
    store = _open_store(args.store)
    try:
        if args.all_captures:
            _print({"operation": "structured_ingest", "runs": ingest_all(store, archive, args.source)})
        else:
            _print({"operation": "structured_ingest", **ingest_latest(store, archive, args.source)})
    finally:
        store.close()
        archive.close()


def run_issue(args):
    store = _open_store(args.store)
    try:
        _print({"operation": "issue_snapshot", **store.issue_snapshot(args.reading, args.provider)})
    finally:
        store.close()


def run_accept(args):
    store = _open_store(args.store)
    try:
        _print({"operation": "accept_adjustment", **store.accept_suggestion(args.suggestion_id, args.note)})
    finally:
        store.close()


def run_finance(args):
    store = _open_store(args.store)
    try:
        if args.finance_command == "status":
            _print({"operation": "finance_status", "series": store.series_overview()})
        else:
            _print(
                {
                    "operation": "finance_difference",
                    **store.difference_detail(args.snapshot_id),
                }
            )
    finally:
        store.close()


def run_demo():
    with tempfile.TemporaryDirectory() as temporary, serve() as origin:
        directory = Path(temporary)
        raw_path = directory / "raw.sqlite3"
        structured_path = directory / "readings.sqlite3"

        archived = capture(origin.base_url, raw_path, "local-source", "/feed")
        archive = Archive(raw_path)
        store = IngestStore(structured_path)
        try:
            first_pass = ingest_latest(store, archive)
            # 同一报文重复投递：事实命中唯一约束，调整建议不重复生成。
            repeat_pass = ingest_latest(store, archive)

            # 月度签发时 R10 的当前解释是 rev2（实测），R11 倍率待核仍可冻结但影响挂起。
            snapshot_r10 = store.issue_snapshot("R10", "grid-a")
            snapshot_r11 = store.issue_snapshot("R11", "grid-a")

            # 财务并列视图：已签发读数、当前解释、待核影响。
            finance_view = store.series_overview()

            _print(
                {
                    "operation": "structured_demo",
                    "raw_capture": archived,
                    "first_ingest": first_pass,
                    "repeat_ingest": {
                        "duplicated_facts": sum(1 for item in repeat_pass["ingested"] if item["duplicated"]),
                        "suggestion_count": len(repeat_pass["suggestions"]),
                    },
                    "issued_snapshots": [snapshot_r10, snapshot_r11],
                    "finance_status": finance_view,
                }
            )
        finally:
            store.close()
            archive.close()


def main():
    parser = argparse.ArgumentParser(description="来源响应接收、归档与读数结构化接入")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("demo")
    collector = subcommands.add_parser("capture")
    collector.add_argument("--base-url", required=True)
    collector.add_argument("--archive", type=Path, required=True)
    collector.add_argument("--source", required=True)
    collector.add_argument("--path", default="/feed")

    ingest = subcommands.add_parser("ingest", help="把已归档报文结构化接入读数库")
    ingest.add_argument("--archive", type=Path, required=True)
    ingest.add_argument("--store", type=Path, required=True)
    ingest.add_argument("--source")
    ingest.add_argument("--all-captures", action="store_true")
    ingest.set_defaults(func=run_ingest)

    issue = subcommands.add_parser("issue", help="按当前解释冻结月度签发快照")
    issue.add_argument("--store", type=Path, required=True)
    issue.add_argument("--reading", required=True)
    issue.add_argument("--provider")
    issue.set_defaults(func=run_issue)

    accept = subcommands.add_parser("accept", help="财务接受一笔调整建议并冻结")
    accept.add_argument("--store", type=Path, required=True)
    accept.add_argument("--suggestion-id", type=int, required=True)
    accept.add_argument("--note")
    accept.set_defaults(func=run_accept)

    finance = subcommands.add_parser("finance", help="财务查询：并列视图与差异回溯")
    finance_sub = finance.add_subparsers(dest="finance_command", required=True)
    status = finance_sub.add_parser("status")
    status.add_argument("--store", type=Path, required=True)
    difference = finance_sub.add_parser("difference")
    difference.add_argument("--store", type=Path, required=True)
    difference.add_argument("--snapshot-id", type=int, required=True)
    finance.set_defaults(func=run_finance)

    server = subcommands.add_parser("serve")
    server.add_argument("--scenario", type=Path)
    args = parser.parse_args()
    if args.command == "demo":
        run_demo()
    elif args.command == "capture":
        _print(capture(args.base_url, args.archive, args.source, args.path))
    elif args.command == "serve":
        from .origin import load_scenario
        with serve(load_scenario(args.scenario)) as origin:
            _print({"base_url": origin.base_url})
            try:
                import threading
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
    else:
        args.func(args)


if __name__ == "__main__":
    main()
