"""读数结构化台账：在原始归档之上建立可解释、可溯源的业务层。

设计约束（对应业务规则）：

* ``reading_messages`` 只追加，供方报文与修订永不覆盖；同一修订号内容不同
  视为报文冲突并报错，绝不静默替换。
* 当前解释（``interpretations``）按最大修订号重算，乱序到达也能收敛；
  每次解释落一条不可变版本（``interpretation_versions``）。
* 已签发快照及其读数副本由数据库触发器禁止更新和删除；后续更正只产生
  调整建议，不改历史账。
* 调整建议按“快照 + 读数 + 当前解释摘要”去重，重复投递不产生第二笔。
* 未知身份或未知倍率一律待核，代码路径中不存在默认成一的分支。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .ingest import normalize_batch
from .rules import (
    IDENTITY_PENDING,
    IDENTITY_RESOLVED,
    MULTIPLIER_KNOWN,
    MULTIPLIER_PENDING,
    RuleBook,
    canonical_json,
    digest_of,
    parse_timestamp,
    utc_now,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_versions (
    rule_set TEXT NOT NULL,
    version INTEGER NOT NULL,
    digest TEXT NOT NULL,
    content TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (rule_set, version),
    UNIQUE (rule_set, digest)
);
CREATE TABLE IF NOT EXISTS meter_installs (
    rule_set TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    provider TEXT NOT NULL,
    meter_label TEXT NOT NULL,
    install_id TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    PRIMARY KEY (rule_set, rule_version, provider, meter_label, valid_from),
    FOREIGN KEY (rule_set, rule_version) REFERENCES rule_versions(rule_set, version)
);
CREATE TABLE IF NOT EXISTS reading_messages (
    message_id INTEGER PRIMARY KEY,
    capture_id INTEGER,
    source TEXT,
    record_index INTEGER,
    provider TEXT NOT NULL,
    meter_label TEXT NOT NULL,
    reading_key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    period TEXT NOT NULL,
    register_value TEXT NOT NULL,
    quality TEXT NOT NULL,
    declared_factor TEXT,
    payload_digest TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    received_seq INTEGER NOT NULL,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (provider, meter_label, reading_key, revision)
);
CREATE TABLE IF NOT EXISTS interpretation_versions (
    version_id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    meter_label TEXT NOT NULL,
    reading_key TEXT NOT NULL,
    interpretation_digest TEXT NOT NULL,
    revision INTEGER NOT NULL,
    message_id INTEGER NOT NULL REFERENCES reading_messages(message_id),
    rule_set TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    install_id TEXT,
    identity_status TEXT NOT NULL,
    multiplier_status TEXT NOT NULL,
    factor TEXT,
    multiplier_reason TEXT NOT NULL,
    gross_value TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (provider, meter_label, reading_key, interpretation_digest)
);
CREATE TABLE IF NOT EXISTS interpretations (
    interpretation_id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    meter_label TEXT NOT NULL,
    reading_key TEXT NOT NULL,
    period TEXT NOT NULL,
    install_id TEXT,
    identity_status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    message_id INTEGER NOT NULL REFERENCES reading_messages(message_id),
    version_id INTEGER NOT NULL REFERENCES interpretation_versions(version_id),
    rule_set TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    register_value TEXT NOT NULL,
    quality TEXT NOT NULL,
    declared_factor TEXT,
    multiplier_status TEXT NOT NULL,
    factor TEXT,
    multiplier_reason TEXT NOT NULL,
    gross_value TEXT,
    interpretation_digest TEXT NOT NULL,
    rule_basis_id TEXT NOT NULL,
    superseded_by INTEGER REFERENCES interpretation_versions(version_id),
    updated_at TEXT NOT NULL,
    UNIQUE (provider, meter_label, reading_key)
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id INTEGER PRIMARY KEY,
    account_period TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    rule_set TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    snapshot_digest TEXT NOT NULL,
    UNIQUE (account_period, snapshot_digest)
);
CREATE TABLE IF NOT EXISTS snapshot_readings (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
    reading_key TEXT NOT NULL,
    provider TEXT NOT NULL,
    meter_label TEXT NOT NULL,
    install_id TEXT,
    identity_status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    interpretation_digest TEXT NOT NULL,
    register_value TEXT NOT NULL,
    quality TEXT NOT NULL,
    multiplier_status TEXT NOT NULL,
    factor TEXT,
    gross_value TEXT,
    rule_basis_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, reading_key)
);
CREATE TRIGGER IF NOT EXISTS snapshots_no_update
BEFORE UPDATE ON snapshots BEGIN SELECT RAISE(ABORT,'snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_delete
BEFORE DELETE ON snapshots BEGIN SELECT RAISE(ABORT,'snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_readings_no_update
BEFORE UPDATE ON snapshot_readings BEGIN SELECT RAISE(ABORT,'issued readings are immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_readings_no_delete
BEFORE DELETE ON snapshot_readings BEGIN SELECT RAISE(ABORT,'issued readings are immutable'); END;
CREATE TABLE IF NOT EXISTS adjustment_suggestions (
    suggestion_id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
    reading_key TEXT NOT NULL,
    current_version_id INTEGER NOT NULL REFERENCES interpretation_versions(version_id),
    interpretation_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('proposed','accepted')),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decision_note TEXT,
    prior_suggestion_id INTEGER REFERENCES adjustment_suggestions(suggestion_id),
    issued_register TEXT NOT NULL,
    issued_quality TEXT NOT NULL,
    issued_factor TEXT,
    issued_gross TEXT,
    current_register TEXT NOT NULL,
    current_quality TEXT NOT NULL,
    current_factor TEXT,
    current_gross TEXT,
    register_delta TEXT NOT NULL,
    gross_delta TEXT,
    delta_vs_prior_register TEXT,
    delta_vs_prior_gross TEXT,
    change_summary TEXT NOT NULL,
    UNIQUE (snapshot_id, reading_key, interpretation_digest)
);
CREATE INDEX IF NOT EXISTS idx_messages_reading
    ON reading_messages(provider, meter_label, reading_key, revision);
CREATE INDEX IF NOT EXISTS idx_adjustments_snapshot
    ON adjustment_suggestions(snapshot_id, reading_key);
"""


class ReadingConflict(ValueError):
    """同一来源修订号出现了不同内容，需人工核对供方报文。"""


class SnapshotError(ValueError):
    """快照签发或调整流程不满足前置条件。"""


def _plain(number: Decimal | None):
    if number is None:
        return None
    return format(number, "f")


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        with self.connection:
            yield self.connection

    # ----- 规则版本 -------------------------------------------------------

    def publish_rules(self, content: dict, *, rule_set: str = "default",
                      published_at: str | None = None) -> RuleBook:
        existing = self.latest_rulebook(rule_set)
        version = 1 if existing is None else existing.version + 1
        book = RuleBook.create(content, rule_set=rule_set, version=version,
                               published_at=published_at)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT version FROM rule_versions WHERE rule_set=? AND digest=?",
                (rule_set, book.digest),
            ).fetchone()
            if row is not None:
                return self._load_rulebook(conn, rule_set, row["version"])
            conn.execute(
                "INSERT INTO rule_versions(rule_set,version,digest,content,published_at)"
                " VALUES (?,?,?,?,?)",
                (rule_set, version, book.digest, canonical_json(book.content), book.published_at),
            )
            for item in book.meter_intervals():
                conn.execute(
                    "INSERT INTO meter_installs(rule_set,rule_version,provider,meter_label,"
                    "install_id,valid_from,valid_to) VALUES (?,?,?,?,?,?,?)",
                    (rule_set, version, item.provider, item.meter_label, item.install_id,
                     item.valid_from.isoformat().replace("+00:00", "Z"),
                     None if item.valid_to is None
                     else item.valid_to.isoformat().replace("+00:00", "Z")),
                )
        return book

    def latest_rulebook(self, rule_set: str = "default") -> RuleBook | None:
        row = self.connection.execute(
            "SELECT rule_set,version FROM rule_versions WHERE rule_set=?"
            " ORDER BY version DESC LIMIT 1",
            (rule_set,),
        ).fetchone()
        if row is None:
            return None
        with self.transaction() as conn:
            return self._load_rulebook(conn, row["rule_set"], row["version"])

    @staticmethod
    def _load_rulebook(conn, rule_set, version) -> RuleBook:
        row = conn.execute(
            "SELECT * FROM rule_versions WHERE rule_set=? AND version=?",
            (rule_set, version),
        ).fetchone()
        content = json.loads(row["content"])
        return RuleBook(rule_set, version, row["published_at"], content, row["digest"])

    # ----- 结构化接入 -----------------------------------------------------

    def ingest_records(self, records: list[dict], *, capture_id: int | None = None,
                       source: str | None = None,
                       rule_book: RuleBook | None = None) -> dict:
        """接入一批供方记录；重复消息跳过，冲突消息报错且整批不落库。"""
        book = rule_book or self.latest_rulebook()
        if book is None:
            raise SnapshotError("publish a rule version before ingesting readings")
        messages = normalize_batch(records)
        ingested, duplicates, conflicts = [], [], []
        with self.transaction() as conn:
            for index, message in enumerate(messages):
                row = conn.execute(
                    "SELECT payload_digest FROM reading_messages"
                    " WHERE provider=? AND meter_label=? AND reading_key=? AND revision=?",
                    message.message_key,
                ).fetchone()
                if row is not None:
                    if row["payload_digest"] != message.payload_digest:
                        conflicts.append(message.message_key)
                    else:
                        duplicates.append(message.message_key)
                    continue
                seq = conn.execute(
                    "SELECT COALESCE(MAX(received_seq),0)+1 AS seq FROM reading_messages"
                ).fetchone()["seq"]
                cursor = conn.execute(
                    "INSERT INTO reading_messages(capture_id,source,record_index,provider,"
                    "meter_label,reading_key,revision,observed_at,period,register_value,"
                    "quality,declared_factor,payload_digest,raw_payload,received_seq)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (capture_id, source, index, message.provider, message.meter_label,
                     message.reading_key, message.revision, message.observed_at,
                     message.period, _plain(message.register_value), message.quality,
                     _plain(message.declared_factor), message.payload_digest,
                     canonical_json(message.raw), seq),
                )
                ingested.append((cursor.lastrowid, message))
            if conflicts:
                raise ReadingConflict(
                    "conflicting payload for already-seen revision(s): "
                    + ", ".join("/".join(map(str, key)) for key in conflicts)
                )
        for _message_id, message in ingested:
            self._recompute(message.provider, message.meter_label, message.reading_key, book)
        return {
            "ingested": len(ingested),
            "duplicates": len(duplicates),
            "conflicts": len(conflicts),
            "total": len(messages),
        }

    def reinterpret_all(self, rule_book: RuleBook | None = None) -> int:
        """规则换版后按新规则重算全部当前解释，返回重算的读数条数。"""
        book = rule_book or self.latest_rulebook()
        if book is None:
            raise SnapshotError("no rule version available")
        rows = self.connection.execute(
            "SELECT DISTINCT provider,meter_label,reading_key FROM interpretations"
        ).fetchall()
        for row in rows:
            self._recompute(row["provider"], row["meter_label"],
                            row["reading_key"], book)
        return len(rows)

    def _recompute(self, provider: str, meter_label: str, reading_key: str,
                   book: RuleBook) -> sqlite3.Row:
        with self.transaction() as conn:
            latest = conn.execute(
                "SELECT * FROM reading_messages WHERE provider=? AND meter_label=?"
                " AND reading_key=? ORDER BY revision DESC LIMIT 1",
                (provider, meter_label, reading_key),
            ).fetchone()
            if latest is None:
                raise KeyError(reading_key)
            observed_at = parse_timestamp(latest["observed_at"])
            install_id = book.resolve_install(provider, meter_label, observed_at)
            identity_status = IDENTITY_RESOLVED if install_id else IDENTITY_PENDING
            factor, multiplier_status, reason, gross = self._resolve_multiplier(
                conn, book, install_id, identity_status, observed_at, latest
            )
            digest_payload = {
                "provider": provider,
                "meter": meter_label,
                "reading": reading_key,
                "revision": latest["revision"],
                "message_id": latest["message_id"],
                "rule": book.id,
                "rule_digest": book.digest,
                "install": install_id,
                "identity_status": identity_status,
                "register_value": latest["register_value"],
                "quality": latest["quality"],
                "multiplier_status": multiplier_status,
                "factor": _plain(factor),
                "gross_value": _plain(gross),
            }
            interpretation_digest = digest_of(digest_payload)
            current = conn.execute(
                "SELECT * FROM interpretations WHERE provider=? AND meter_label=?"
                " AND reading_key=?",
                (provider, meter_label, reading_key),
            ).fetchone()
            if current is not None and current["interpretation_digest"] == interpretation_digest:
                return current
            conn.execute(
                "INSERT OR IGNORE INTO interpretation_versions(provider,meter_label,"
                "reading_key,interpretation_digest,revision,message_id,rule_set,"
                "rule_version,install_id,identity_status,multiplier_status,factor,"
                "multiplier_reason,gross_value,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (provider, meter_label, reading_key, interpretation_digest,
                 latest["revision"], latest["message_id"], book.rule_set, book.version,
                 install_id, identity_status, multiplier_status, _plain(factor), reason,
                 _plain(gross), utc_now()),
            )
            version = conn.execute(
                "SELECT version_id FROM interpretation_versions WHERE provider=?"
                " AND meter_label=? AND reading_key=? AND interpretation_digest=?",
                (provider, meter_label, reading_key, interpretation_digest),
            ).fetchone()
            if current is not None:
                conn.execute(
                    "UPDATE interpretations SET superseded_by=? WHERE interpretation_id=?",
                    (version["version_id"], current["interpretation_id"]),
                )
                conn.execute(
                    "UPDATE interpretations SET period=?,install_id=?,identity_status=?,"
                    "revision=?,message_id=?,version_id=?,rule_set=?,rule_version=?,"
                    "observed_at=?,register_value=?,quality=?,declared_factor=?,"
                    "multiplier_status=?,factor=?,multiplier_reason=?,gross_value=?,"
                    "interpretation_digest=?,rule_basis_id=?,superseded_by=NULL,updated_at=?"
                    " WHERE interpretation_id=?",
                    (latest["period"], install_id, identity_status, latest["revision"],
                     latest["message_id"], version["version_id"], book.rule_set, book.version,
                     latest["observed_at"], latest["register_value"], latest["quality"],
                     latest["declared_factor"], multiplier_status, _plain(factor), reason,
                     _plain(gross), interpretation_digest, book.id, utc_now(),
                     current["interpretation_id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO interpretations(provider,meter_label,reading_key,period,"
                    "install_id,identity_status,revision,message_id,version_id,rule_set,"
                    "rule_version,observed_at,register_value,quality,declared_factor,"
                    "multiplier_status,factor,multiplier_reason,gross_value,"
                    "interpretation_digest,rule_basis_id,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (provider, meter_label, reading_key, latest["period"], install_id,
                     identity_status, latest["revision"], latest["message_id"],
                     version["version_id"], book.rule_set, book.version,
                     latest["observed_at"], latest["register_value"], latest["quality"],
                     latest["declared_factor"], multiplier_status, _plain(factor), reason,
                     _plain(gross), interpretation_digest, book.id, utc_now()),
                )
            return conn.execute(
                "SELECT * FROM interpretations WHERE provider=? AND meter_label=?"
                " AND reading_key=?",
                (provider, meter_label, reading_key),
            ).fetchone()

    @staticmethod
    def _resolve_multiplier(conn, book: RuleBook, install_id, identity_status,
                           observed_at: datetime, message_row):
        declared = (None if message_row["declared_factor"] is None
                    else Decimal(message_row["declared_factor"]))
        if identity_status == IDENTITY_PENDING:
            return None, MULTIPLIER_PENDING, "安装身份待核，倍率不得套用", None
        rule_factor = book.resolve_factor(install_id, observed_at)
        if rule_factor is None:
            return None, MULTIPLIER_PENDING, "适用规则中没有已确认的倍率生效区间", None
        if declared is None:
            gross = Decimal(message_row["register_value"]) * rule_factor
            return rule_factor, MULTIPLIER_KNOWN, "按规则生效区间确认；供方报文未申报倍率", gross
        if declared != rule_factor:
            return None, MULTIPLIER_PENDING, (
                "供方申报倍率 " + _plain(declared) + " 与规则生效区间 "
                + _plain(rule_factor) + " 不一致，待核"
            ), None
        gross = Decimal(message_row["register_value"]) * rule_factor
        return rule_factor, MULTIPLIER_KNOWN, "供方申报与规则生效区间一致", gross

    # ----- 已签发快照（不可变） -------------------------------------------

    def issue_snapshot(self, account_period: str, *, note: str = "",
                       issued_at: str | None = None,
                       rule_book: RuleBook | None = None) -> dict:
        self._check_period(account_period)
        book = rule_book or self.latest_rulebook()
        if book is None:
            raise SnapshotError("no rule version available")
        issued_at = issued_at or utc_now()
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM interpretations WHERE period=? ORDER BY reading_key",
                (account_period,),
            ).fetchall()
            if not rows:
                raise SnapshotError("no readings for period " + account_period)
            snapshot_digest = digest_of([
                {"reading": row["reading_key"], "digest": row["interpretation_digest"]}
                for row in rows
            ])
            existing = conn.execute(
                "SELECT snapshot_id FROM snapshots WHERE account_period=? AND snapshot_digest=?",
                (account_period, snapshot_digest),
            ).fetchone()
            if existing is not None:
                return {"snapshot_id": existing["snapshot_id"], "reused": True,
                        "readings": len(rows)}
            cursor = conn.execute(
                "INSERT INTO snapshots(account_period,issued_at,note,rule_set,"
                "rule_version,snapshot_digest) VALUES (?,?,?,?,?,?)",
                (account_period, issued_at, note, book.rule_set, book.version, snapshot_digest),
            )
            snapshot_id = cursor.lastrowid
            for row in rows:
                conn.execute(
                    "INSERT INTO snapshot_readings(snapshot_id,reading_key,provider,"
                    "meter_label,install_id,identity_status,revision,"
                    "interpretation_digest,register_value,quality,multiplier_status,"
                    "factor,gross_value,rule_basis_id,message_id)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (snapshot_id, row["reading_key"], row["provider"], row["meter_label"],
                     row["install_id"], row["identity_status"], row["revision"],
                     row["interpretation_digest"], row["register_value"], row["quality"],
                     row["multiplier_status"], row["factor"], row["gross_value"],
                     row["rule_basis_id"], row["message_id"]),
                )
        return {"snapshot_id": snapshot_id, "reused": False, "readings": len(rows)}

    def snapshots(self) -> list[dict]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM snapshots ORDER BY snapshot_id").fetchall()]

    # ----- 调整建议（幂等、链式） -----------------------------------------

    def generate_adjustments(self, snapshot_id: int) -> dict:
        with self.transaction() as conn:
            snapshot = conn.execute(
                "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,),
            ).fetchone()
            if snapshot is None:
                raise SnapshotError("unknown snapshot: " + str(snapshot_id))
            issued_rows = conn.execute(
                "SELECT * FROM snapshot_readings WHERE snapshot_id=? ORDER BY reading_key",
                (snapshot_id,),
            ).fetchall()
            created, unchanged, already = [], 0, []
            for issued in issued_rows:
                current = conn.execute(
                    "SELECT * FROM interpretations WHERE provider=? AND meter_label=?"
                    " AND reading_key=?",
                    (issued["provider"], issued["meter_label"], issued["reading_key"]),
                ).fetchone()
                if current["interpretation_digest"] == issued["interpretation_digest"]:
                    unchanged += 1
                    continue
                existing = conn.execute(
                    "SELECT * FROM adjustment_suggestions WHERE snapshot_id=?"
                    " AND reading_key=? AND interpretation_digest=?",
                    (snapshot_id, issued["reading_key"], current["interpretation_digest"]),
                ).fetchone()
                if existing is not None:
                    already.append(dict(existing))
                    continue
                prior = conn.execute(
                    "SELECT * FROM adjustment_suggestions WHERE snapshot_id=?"
                    " AND reading_key=? ORDER BY suggestion_id DESC LIMIT 1",
                    (snapshot_id, issued["reading_key"]),
                ).fetchone()
                suggestion = self._build_suggestion(
                    conn, snapshot, issued, current, prior,
                )
                created.append(suggestion)
        return {"snapshot_id": snapshot_id, "created": created,
                "deduplicated": len(already), "unchanged": unchanged,
                "existing": already}

    @staticmethod
    def _build_suggestion(conn, snapshot, issued, current, prior) -> dict:
        issued_register = Decimal(issued["register_value"])
        current_register = Decimal(current["register_value"])
        register_delta = current_register - issued_register
        issued_gross = None if issued["gross_value"] is None else Decimal(issued["gross_value"])
        current_gross = None if current["gross_value"] is None else Decimal(current["gross_value"])
        gross_delta = None if (issued_gross is None or current_gross is None) \
            else current_gross - issued_gross
        delta_prior_register = delta_prior_gross = None
        if prior is not None:
            delta_prior_register = current_register - Decimal(prior["current_register"])
            if prior["current_gross"] is not None and current_gross is not None:
                delta_prior_gross = current_gross - Decimal(prior["current_gross"])
        changes = []
        if issued["revision"] != current["revision"]:
            changes.append("读数修订 %d→%d" % (issued["revision"], current["revision"]))
        if issued["quality"] != current["quality"]:
            changes.append("质量 %s→%s" % (issued["quality"], current["quality"]))
        if issued["multiplier_status"] != current["multiplier_status"] \
                or issued["factor"] != current["factor"]:
            changes.append("倍率 %s/%s→%s/%s" % (
                issued["multiplier_status"], issued["factor"] or "?",
                current["multiplier_status"], current["factor"] or "?",
            ))
        if issued["install_id"] != current["install_id"]:
            changes.append("安装身份 %s→%s" % (
                issued["install_id"] or "待核", current["install_id"] or "待核"))
        version = conn.execute(
            "SELECT version_id FROM interpretation_versions WHERE provider=?"
            " AND meter_label=? AND reading_key=? AND interpretation_digest=?",
            (current["provider"], current["meter_label"], current["reading_key"],
             current["interpretation_digest"]),
        ).fetchone()
        cursor = conn.execute(
            "INSERT INTO adjustment_suggestions(snapshot_id,reading_key,current_version_id,"
            "interpretation_digest,status,created_at,prior_suggestion_id,issued_register,"
            "issued_quality,issued_factor,issued_gross,current_register,current_quality,"
            "current_factor,current_gross,register_delta,gross_delta,"
            "delta_vs_prior_register,delta_vs_prior_gross,change_summary)"
            " VALUES (?,?,?,?, 'proposed', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot["snapshot_id"], issued["reading_key"], version["version_id"],
             current["interpretation_digest"], utc_now(),
             None if prior is None else prior["suggestion_id"],
             _plain(issued_register), issued["quality"], issued["factor"],
             _plain(issued_gross), _plain(current_register), current["quality"],
             current["factor"], _plain(current_gross), _plain(register_delta),
             _plain(gross_delta), _plain(delta_prior_register),
             _plain(delta_prior_gross), "；".join(changes)),
        )
        return dict(conn.execute(
            "SELECT * FROM adjustment_suggestions WHERE suggestion_id=?",
            (cursor.lastrowid,),
        ).fetchone())

    def accept_adjustment(self, suggestion_id: int, *, note: str = "",
                          decided_at: str | None = None) -> dict:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM adjustment_suggestions WHERE suggestion_id=?",
                (suggestion_id,),
            ).fetchone()
            if row is None:
                raise SnapshotError("unknown suggestion: " + str(suggestion_id))
            if row["status"] != "proposed":
                raise SnapshotError(
                    "suggestion already " + row["status"] + "; decisions cannot be rewritten")
            conn.execute(
                "UPDATE adjustment_suggestions SET status='accepted', decided_at=?, "
                "decision_note=? WHERE suggestion_id=?",
                (decided_at or utc_now(), note, suggestion_id),
            )
            return dict(conn.execute(
                "SELECT * FROM adjustment_suggestions WHERE suggestion_id=?",
                (suggestion_id,),
            ).fetchone())

    def suggestions(self, snapshot_id: int | None = None) -> list[dict]:
        query = "SELECT * FROM adjustment_suggestions"
        params = ()
        if snapshot_id is not None:
            query += " WHERE snapshot_id=?"
            params = (snapshot_id,)
        return [dict(row) for row in self.connection.execute(
            query + " ORDER BY suggestion_id", params).fetchall()]

    # ----- 报文溯源 -------------------------------------------------------

    def message(self, message_id: int) -> dict:
        row = self.connection.execute(
            "SELECT * FROM reading_messages WHERE message_id=?", (message_id,),
        ).fetchone()
        if row is None:
            raise KeyError(message_id)
        payload = json.loads(row["raw_payload"])
        if digest_of(payload) != row["payload_digest"]:
            raise ValueError("stored payload digest mismatch for message " + str(message_id))
        return {**dict(row), "payload": payload}

    def interpretations(self, period: str | None = None) -> list[dict]:
        query = "SELECT * FROM interpretations"
        params = ()
        if period is not None:
            query += " WHERE period=?"
            params = (period,)
        return [dict(row) for row in self.connection.execute(
            query + " ORDER BY period, reading_key", params).fetchall()]

    def interpretation_versions(self, reading_key: str) -> list[dict]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM interpretation_versions WHERE reading_key=?"
            " ORDER BY version_id", (reading_key,)).fetchall()]

    @staticmethod
    def _check_period(period: str) -> None:
        year_text, separator, month_text = period.partition("-")
        if (separator != "-" or len(year_text) != 4 or len(month_text) != 2
                or not (year_text + month_text).isdigit()
                or not 1 <= int(month_text) <= 12):
            raise ValueError("period must use YYYY-MM: " + period)
