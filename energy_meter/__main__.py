import argparse
import json
import tempfile
from pathlib import Path

from .archive import Archive
from .client import Client, collect
from .cli import build_parser, run_demo
from .decoding import records
from .origin import serve


def capture(base_url, archive_path, source, path):
    archive = Archive(archive_path)
    try:
        capture_id, response = collect(Client(base_url), archive, source, path)
        result = {"capture_id": capture_id, "status": response.status,
                  "bytes": len(response.body), "url": response.url}
        if response.status == 200:
            result["source_records"] = records(response.body,
                                               response.headers.get("content-type", ""))
        result["archived_observations"] = len(archive.captures())
        return result
    finally:
        archive.close()


def main():
    parser = argparse.ArgumentParser(description="来源响应归档与读数结构化接入")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("demo")
    collector = subcommands.add_parser("capture")
    collector.add_argument("--base-url", required=True)
    collector.add_argument("--archive", type=Path, required=True)
    collector.add_argument("--source", required=True)
    collector.add_argument("--path", default="/feed")
    server = subcommands.add_parser("serve")
    server.add_argument("--scenario", type=Path)
    build_parser(subcommands)

    args = parser.parse_args()
    if args.command == "demo":
        print(json.dumps({"operation": "structured_metering_demo",
                          **run_demo()}, ensure_ascii=False, indent=2))
    elif args.command == "capture":
        print(json.dumps(
            {"operation": "raw_capture",
             **capture(args.base_url, args.archive, args.source, args.path)},
            ensure_ascii=False, indent=2))
    elif args.command == "serve":
        from .origin import load_scenario
        with serve(load_scenario(args.scenario)) as origin:
            print(json.dumps({"base_url": origin.base_url},
                             ensure_ascii=False), flush=True)
            try:
                import threading
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
    else:
        print(json.dumps(args.handler(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
