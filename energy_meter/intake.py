"""把已归档的供方报文结构化接入，不改动原始归档。

每条结构化事实都回链归档的 capture 标识与报文摘要，财务从差异可回到
具体报文；接入过程中无法规范化的记录单独报错跳过，不污染其余事实。
"""
from .archive import Archive
from .decoding import records as decode_records
from .domain import ReadingRecordError
from .store import IngestStore


def capture_context(archive: Archive, capture_row) -> dict:
    return {
        "capture_id": capture_row["id"],
        "source": capture_row["source"],
        "url": capture_row["url"],
        "digest": capture_row["digest"],
        "received_at": capture_row["received_at"],
    }


def ingest_archived_capture(store: IngestStore, archive: Archive, capture_row) -> dict:
    body = archive.body(capture_row["id"])
    content_type = capture_row["headers"].get("content-type", "")
    source_records = decode_records(body, content_type)
    context = capture_context(archive, capture_row)
    ingested, rejected = [], []
    for raw in source_records:
        try:
            ingested.append(store.ingest_record(raw, context))
        except ReadingRecordError as error:
            rejected.append({"record": raw, "reason": str(error)})
    suggestions = store.refresh_suggestions()
    return {
        "capture_id": capture_row["id"],
        "digest": context["digest"],
        "records": len(source_records),
        "ingested": ingested,
        "rejected": rejected,
        "suggestions": suggestions,
    }


def ingest_latest(store: IngestStore, archive: Archive, source: str | None = None) -> dict:
    rows = archive.captures(source)
    if not rows:
        raise ValueError("no archived capture available")
    return ingest_archived_capture(store, archive, rows[-1])


def ingest_all(store: IngestStore, archive: Archive, source: str | None = None) -> list[dict]:
    return [
        ingest_archived_capture(store, archive, row)
        for row in archive.captures(source)
    ]
