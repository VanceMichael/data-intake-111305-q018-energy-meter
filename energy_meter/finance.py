"""面向财务的只读查询：不计算费用、不重写历史，只并列呈现与溯源。

三类视图：

* :func:`finance_comparison` —— 已签发读数 / 当前解释 / 待核影响并列；
* :func:`pending_impacts` —— 身份或倍率待核、金额影响尚不能确认的读数；
* :func:`trace_reading` —— 从一个读数或一笔差异回到每条供方报文与适用规则；
* :func:`meter_series` —— 按内部安装身份组织的累计曲线，换表即边界，
  设备编号复用不会被接成同一条曲线。
"""
from __future__ import annotations

from decimal import Decimal

from .rules import IDENTITY_PENDING, MULTIPLIER_PENDING


def _number(value):
    return None if value is None else Decimal(value)


def finance_comparison(ledger, snapshot_id: int, *, include_unissued: bool = True) -> dict:
    conn = ledger.connection
    snapshot = conn.execute(
        "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,),
    ).fetchone()
    if snapshot is None:
        raise KeyError(snapshot_id)
    rows = []
    issued_rows = conn.execute(
        "SELECT * FROM snapshot_readings WHERE snapshot_id=? ORDER BY reading_key",
        (snapshot_id,),
    ).fetchall()
    seen = set()
    for issued in issued_rows:
        seen.add(issued["reading_key"])
        current = conn.execute(
            "SELECT * FROM interpretations WHERE provider=? AND meter_label=?"
            " AND reading_key=?",
            (issued["provider"], issued["meter_label"], issued["reading_key"]),
        ).fetchone()
        suggestion = conn.execute(
            "SELECT * FROM adjustment_suggestions WHERE snapshot_id=? AND reading_key=?"
            " ORDER BY suggestion_id DESC LIMIT 1",
            (snapshot_id, issued["reading_key"]),
        ).fetchone()
        rows.append(_comparison_row(dict(issued), dict(current), suggestion))
    if include_unissued:
        for current in conn.execute(
            "SELECT * FROM interpretations WHERE period=? ORDER BY reading_key",
            (snapshot["account_period"],),
        ).fetchall():
            if current["reading_key"] in seen:
                continue
            rows.append(_comparison_row(None, dict(current), None))
    return {
        "snapshot": dict(snapshot),
        "rule_basis": _rule_basis(ledger, snapshot["rule_set"], snapshot["rule_version"]),
        "readings": rows,
        "summary": {
            "total": len(rows),
            "changed": sum(1 for row in rows if row["impact"]["changed"]),
            "pending": sum(1 for row in rows if row["impact"]["pending"]),
            "proposed_adjustments": sum(
                1 for row in rows
                if row["adjustment"] and row["adjustment"]["status"] == "proposed"),
            "accepted_adjustments": sum(
                1 for row in rows
                if row["adjustment"] and row["adjustment"]["status"] == "accepted"),
        },
    }


def _comparison_row(issued: dict | None, current: dict, suggestion) -> dict:
    if issued is None:
        issued_view = None
        impact = {
            "changed": True,
            "pending": _is_pending(current),
            "register_delta": current["register_value"],
            "gross_delta": current["gross_value"],
            "note": "该读数在快照签发后才形成当前解释，尚未纳入任何已签发账",
        }
    else:
        issued_view = {
            "reading_key": issued["reading_key"],
            "install_id": issued["install_id"],
            "identity_status": issued["identity_status"],
            "revision": issued["revision"],
            "register_value": issued["register_value"],
            "quality": issued["quality"],
            "multiplier_status": issued["multiplier_status"],
            "factor": issued["factor"],
            "gross_value": issued["gross_value"],
            "rule_basis_id": issued["rule_basis_id"],
            "message_id": issued["message_id"],
        }
        register_delta = _number(current["register_value"]) - _number(issued["register_value"])
        gross_delta = None
        if issued["gross_value"] is not None and current["gross_value"] is not None:
            gross_delta = _number(current["gross_value"]) - _number(issued["gross_value"])
        impact = {
            "changed": current["interpretation_digest"] != issued["interpretation_digest"],
            "pending": _is_pending(current),
            "register_delta": format(register_delta, "f"),
            "gross_delta": None if gross_delta is None else format(gross_delta, "f"),
            "pending_reason": None if not _is_pending(current) else current["multiplier_reason"],
        }
    current_view = {
        "reading_key": current["reading_key"],
        "provider": current["provider"],
        "meter_label": current["meter_label"],
        "install_id": current["install_id"],
        "identity_status": current["identity_status"],
        "revision": current["revision"],
        "observed_at": current["observed_at"],
        "period": current["period"],
        "register_value": current["register_value"],
        "quality": current["quality"],
        "declared_factor": current["declared_factor"],
        "multiplier_status": current["multiplier_status"],
        "factor": current["factor"],
        "multiplier_reason": current["multiplier_reason"],
        "gross_value": current["gross_value"],
        "rule_basis_id": current["rule_basis_id"],
        "message_id": current["message_id"],
        "version_id": current["version_id"],
    }
    return {
        "reading_key": current["reading_key"],
        "issued": issued_view,
        "current": current_view,
        "impact": impact,
        "adjustment": None if suggestion is None else dict(suggestion),
        "trace": {
            "message_id": current["message_id"],
            "rule_basis_id": current["rule_basis_id"],
            "version_id": current["version_id"],
            "reading_key": current["reading_key"],
        },
    }


def _is_pending(current: dict) -> bool:
    return (current["identity_status"] == IDENTITY_PENDING
            or current["multiplier_status"] == MULTIPLIER_PENDING)


def pending_impacts(ledger, period: str | None = None) -> dict:
    query = "SELECT * FROM interpretations WHERE identity_status=? OR multiplier_status=?"
    params = (IDENTITY_PENDING, MULTIPLIER_PENDING)
    if period is not None:
        query += " AND period=?"
        params += (period,)
    rows = []
    for row in ledger.connection.execute(query + " ORDER BY period, reading_key", params):
        item = dict(row)
        item["blocked_amount"] = True
        item["reason"] = (item["multiplier_reason"]
                          if item["multiplier_status"] == MULTIPLIER_PENDING
                          else "安装身份待核")
        rows.append(item)
    return {"pending_count": len(rows), "readings": rows}


def meter_series(ledger, provider: str, meter_label: str) -> dict:
    """按安装身份列出累计曲线；同一编号的每次复用都是互不相接的序列。"""
    conn = ledger.connection
    installs = conn.execute(
        "SELECT DISTINCT install_id FROM interpretations WHERE provider=? AND meter_label=?"
        " AND install_id IS NOT NULL ORDER BY install_id",
        (provider, meter_label),
    ).fetchall()
    series = []
    for install_row in installs:
        install_id = install_row["install_id"]
        points = [dict(row) for row in conn.execute(
            "SELECT reading_key,observed_at,period,revision,register_value,quality,"
            "multiplier_status,factor,gross_value FROM interpretations"
            " WHERE install_id=? ORDER BY observed_at, reading_key",
            (install_id,),
        )]
        intervals = [dict(row) for row in conn.execute(
            "SELECT rule_version,valid_from,valid_to FROM meter_installs"
            " WHERE provider=? AND meter_label=? AND install_id=? ORDER BY rule_version",
            (provider, meter_label, install_id),
        )]
        series.append({
            "install_id": install_id,
            "points": points,
            "first_reading": points[0]["register_value"] if points else None,
            "last_reading": points[-1]["register_value"] if points else None,
            "install_intervals": intervals,
            "join_allowed_with": [],
        })
    unresolved = [dict(row) for row in conn.execute(
        "SELECT reading_key,observed_at,register_value,multiplier_reason"
        " FROM interpretations WHERE provider=? AND meter_label=?"
        " AND install_id IS NULL ORDER BY observed_at",
        (provider, meter_label),
    )]
    boundaries = []
    for previous, current in zip(series, series[1:]):
        boundaries.append({
            "from_install": previous["install_id"],
            "to_install": current["install_id"],
            "old_meter_tail": previous["last_reading"],
            "new_meter_head": current["first_reading"],
            "rule": "跨安装身份不得相接，换表两侧分别累计",
        })
    return {
        "provider": provider,
        "meter_label": meter_label,
        "series": series,
        "boundaries": boundaries,
        "unresolved_readings": unresolved,
        "reused_label": len(series) > 1,
    }


def trace_reading(ledger, reading_key: str, *, archive=None) -> dict:
    """汇总一个读数的全部依据：供方报文各修订、解释版本、规则与快照、调整链。"""
    conn = ledger.connection
    messages = [dict(row) for row in conn.execute(
        "SELECT message_id,capture_id,source,record_index,revision,observed_at,"
        "register_value,quality,declared_factor,payload_digest,received_seq,received_at"
        " FROM reading_messages WHERE reading_key=? ORDER BY revision, message_id",
        (reading_key,),
    )]
    if not messages:
        raise KeyError(reading_key)
    for item in messages:
        item["payload"] = ledger.message(item["message_id"])["payload"]
        if archive is not None and item["capture_id"] is not None:
            try:
                capture = next(c for c in archive.captures()
                               if c["id"] == item["capture_id"])
                item["source_message"] = {
                    "source": capture["source"],
                    "url": capture["url"],
                    "status": capture["status"],
                    "received_at": capture["received_at"],
                    "body_digest": capture["digest"],
                    "record_index": item["record_index"],
                }
            except StopIteration:
                item["source_message"] = None
        else:
            item["source_message"] = None
    versions = [dict(row) for row in conn.execute(
        "SELECT * FROM interpretation_versions WHERE reading_key=? ORDER BY version_id",
        (reading_key,),
    )]
    rule_basis = {}
    for version in versions:
        key = (version["rule_set"], version["rule_version"])
        if key not in rule_basis:
            rule_basis["%s@%s" % key] = _rule_basis(
                ledger, version["rule_set"], version["rule_version"])
    snapshot_rows = [dict(row) for row in conn.execute(
        "SELECT sr.*,s.account_period,s.issued_at FROM snapshot_readings sr"
        " JOIN snapshots s ON s.snapshot_id=sr.snapshot_id"
        " WHERE sr.reading_key=? ORDER BY sr.snapshot_id",
        (reading_key,),
    )]
    adjustments = [dict(row) for row in conn.execute(
        "SELECT * FROM adjustment_suggestions WHERE reading_key=?"
        " ORDER BY suggestion_id", (reading_key,),
    )]
    return {
        "reading_key": reading_key,
        "supplier_messages": messages,
        "interpretation_versions": versions,
        "applicable_rules": rule_basis,
        "issued_snapshots": snapshot_rows,
        "adjustment_chain": adjustments,
    }


def trace_suggestion(ledger, suggestion_id: int, *, archive=None) -> dict:
    conn = ledger.connection
    suggestion = conn.execute(
        "SELECT * FROM adjustment_suggestions WHERE suggestion_id=?",
        (suggestion_id,),
    ).fetchone()
    if suggestion is None:
        raise KeyError(suggestion_id)
    traced = trace_reading(ledger, suggestion["reading_key"], archive=archive)
    return {"suggestion": dict(suggestion), "trace": traced}


def _rule_basis(ledger, rule_set: str, version: int) -> dict:
    import json

    row = ledger.connection.execute(
        "SELECT * FROM rule_versions WHERE rule_set=? AND version=?",
        (rule_set, version),
    ).fetchone()
    if row is None:
        return {"rule_set": rule_set, "version": version, "missing": True}
    return {
        "rule_set": rule_set,
        "version": version,
        "digest": row["digest"],
        "published_at": row["published_at"],
        "content": json.loads(row["content"]),
    }
