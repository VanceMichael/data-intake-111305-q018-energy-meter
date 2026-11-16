"""读数结构化接入库。

与原始报文归档库分离：本库只追加结构化事实与冻结记录，不写回归档，也不
触碰任何历史账单。当前解释由事实派生，可随乱序补传更新；签发快照与已接受
调整一旦生成即冻结。
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .domain import (
    ReadingRecord,
    RULE_ADJUSTMENT,
    RULE_ADJUSTMENT_CHAIN,
    RULE_CATALOG,
    RULE_MULTIPLIER,
    RULE_REVISION,
    normalize_reading,
    rule_snapshot,
)

PROPOSED = "proposed"
ACCEPTED = "accepted"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


class IngestStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meter_series (
                id INTEGER PRIMARY KEY,
                provider TEXT NOT NULL,
                meter_code TEXT NOT NULL,
                reading_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(provider, meter_code, reading_id)
            );
            CREATE TABLE IF NOT EXISTS reading_facts (
                id INTEGER PRIMARY KEY,
                series_id INTEGER NOT NULL REFERENCES meter_series(id),
                revision INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                period TEXT NOT NULL,
                register_value TEXT NOT NULL,
                quality TEXT NOT NULL,
                multiplier TEXT,
                multiplier_pending INTEGER NOT NULL,
                capture_id INTEGER,
                capture_digest TEXT,
                raw_record TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                UNIQUE(series_id, revision)
            );
            CREATE TABLE IF NOT EXISTS multiplier_intervals (
                id INTEGER PRIMARY KEY,
                series_id INTEGER NOT NULL REFERENCES meter_series(id),
                effective_from TEXT NOT NULL,
                effective_to TEXT,
                multiplier TEXT,
                pending INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS issued_snapshots (
                id INTEGER PRIMARY KEY,
                series_id INTEGER NOT NULL REFERENCES meter_series(id),
                period TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                fact_revision INTEGER NOT NULL,
                register_value TEXT NOT NULL,
                quality TEXT NOT NULL,
                multiplier TEXT,
                multiplier_pending INTEGER NOT NULL,
                billable_value TEXT,
                basis_json TEXT NOT NULL,
                rules_json TEXT NOT NULL,
                UNIQUE(series_id)
            );
            CREATE TABLE IF NOT EXISTS adjustment_suggestions (
                id INTEGER PRIMARY KEY,
                snapshot_id INTEGER NOT NULL REFERENCES issued_snapshots(id),
                series_id INTEGER NOT NULL REFERENCES meter_series(id),
                status TEXT NOT NULL CHECK(status IN ('proposed','accepted')),
                from_revision INTEGER NOT NULL,
                to_revision INTEGER NOT NULL,
                register_delta TEXT NOT NULL,
                billable_delta TEXT,
                basis_json TEXT NOT NULL,
                rules_json TEXT NOT NULL,
                prior_suggestion_id INTEGER REFERENCES adjustment_suggestions(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                accepted_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_suggestion_per_snapshot
                ON adjustment_suggestions(snapshot_id) WHERE status='proposed';
            CREATE TABLE IF NOT EXISTS adjustment_decisions (
                id INTEGER PRIMARY KEY,
                suggestion_id INTEGER NOT NULL REFERENCES adjustment_suggestions(id),
                decision TEXT NOT NULL CHECK(decision IN ('accepted')),
                decided_at TEXT NOT NULL,
                note TEXT
            );
            """
        )

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        with self.connection:
            yield self.connection

    # ------------------------------------------------------------------ 接入
    def _series_id(self, record: ReadingRecord, create: bool = True) -> int | None:
        provider, meter_code, reading_id = record.series_key
        cursor = self.connection.execute(
            "SELECT id FROM meter_series WHERE provider=? AND meter_code=? AND reading_id=?",
            (provider, meter_code, reading_id),
        )
        row = cursor.fetchone()
        if row is not None:
            return row["id"]
        if not create:
            return None
        cursor = self.connection.execute(
            "INSERT INTO meter_series(provider,meter_code,reading_id,created_at) VALUES (?,?,?,?)",
            (provider, meter_code, reading_id, _now()),
        )
        return cursor.lastrowid

    def ingest_record(self, raw: dict, capture: dict | None = None) -> dict:
        """结构化接入一条供方记录。

        同（序列、修订号）的重复投递直接命中唯一约束，不产生第二笔事实，
        也不会触发新的调整建议。乱序到达按修订号并存，当前解释另行派生。
        """
        record = normalize_reading(raw)
        capture_id = None if capture is None else capture.get("capture_id")
        capture_digest = None if capture is None else capture.get("digest")
        with self.transaction():
            series_id = self._series_id(record)
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO reading_facts(
                    series_id,revision,observed_at,period,register_value,quality,
                    multiplier,multiplier_pending,capture_id,capture_digest,
                    raw_record,ingested_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    series_id,
                    record.revision,
                    record.observed_at,
                    record.period,
                    _decimal_text(record.register_value),
                    record.quality,
                    _decimal_text(record.multiplier),
                    1 if record.multiplier is None else 0,
                    capture_id,
                    capture_digest,
                    json.dumps(record.raw, ensure_ascii=False, sort_keys=True),
                    _now(),
                ),
            )
            duplicated = cursor.rowcount == 0
            if not duplicated:
                self._rebuild_intervals(series_id)
        return {
            "series_id": series_id,
            "reading_id": record.reading_id,
            "revision": record.revision,
            "duplicated": duplicated,
        }

    def ingest_records(self, records, capture: dict | None = None) -> list[dict]:
        return [self.ingest_record(raw, capture) for raw in records]

    def _rebuild_intervals(self, series_id: int) -> None:
        """按观测时间重算该序列的倍率生效区间。

        区间由事实本身携带的倍率分段；未知倍率自成待核区间，不被任何已知
        区间覆盖，更不会按一倍率补齐。
        """
        rows = self.connection.execute(
            "SELECT observed_at,multiplier,multiplier_pending FROM reading_facts"
            " WHERE series_id=? ORDER BY observed_at,revision",
            (series_id,),
        ).fetchall()
        self.connection.execute(
            "DELETE FROM multiplier_intervals WHERE series_id=?", (series_id,)
        )
        groups: list[dict] = []
        for row in rows:
            if (
                groups
                and groups[-1]["multiplier"] == row["multiplier"]
                and groups[-1]["pending"] == row["multiplier_pending"]
            ):
                continue
            groups.append(
                {
                    "effective_from": row["observed_at"],
                    "multiplier": row["multiplier"],
                    "pending": row["multiplier_pending"],
                }
            )
        for index, group in enumerate(groups):
            effective_to = groups[index + 1]["effective_from"] if index + 1 < len(groups) else None
            self.connection.execute(
                "INSERT INTO multiplier_intervals(series_id,effective_from,effective_to,multiplier,pending)"
                " VALUES (?,?,?,?,?)",
                (series_id, group["effective_from"], effective_to, group["multiplier"], group["pending"]),
            )

    def _interval_at(self, series_id: int, observed_at: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM multiplier_intervals WHERE series_id=? AND effective_from<=?"
            " AND (effective_to IS NULL OR ?<effective_to) ORDER BY effective_from DESC LIMIT 1",
            (series_id, observed_at, observed_at),
        ).fetchone()

    def current_fact(self, series_id: int) -> sqlite3.Row | None:
        """当前解释：该序列最高修订号的有效事实。"""
        return self.connection.execute(
            "SELECT * FROM reading_facts WHERE series_id=? ORDER BY revision DESC LIMIT 1",
            (series_id,),
        ).fetchone()

    def billable(self, fact: sqlite3.Row) -> Decimal | None:
        """依据事实携带的适用倍率计算账单口径读数。

        待核倍率返回 None，绝不按一处理。倍率生效区间另存于
        ``multiplier_intervals`` 供财务查询展示适用范围。
        """
        if fact["multiplier_pending"]:
            return None
        return Decimal(fact["register_value"]) * Decimal(fact["multiplier"])

    # -------------------------------------------------------------- 签发快照
    def issue_snapshot(self, reading_id: str, provider: str | None = None) -> dict:
        """按当前解释冻结一份签发快照；同一序列只能签发一次。"""
        series = self._require_series(reading_id, provider)
        fact = self.current_fact(series["id"])
        if fact is None:
            raise ValueError(f"no reading fact for {reading_id}")
        if self.connection.execute(
            "SELECT 1 FROM issued_snapshots WHERE series_id=?", (series["id"],)
        ).fetchone():
            raise ValueError(f"snapshot already issued for {reading_id}; history is frozen")
        billable = self.billable(fact)
        basis = {
            "fact_id": fact["id"],
            "reading_id": reading_id,
            "revision": fact["revision"],
            "observed_at": fact["observed_at"],
            "register_value": fact["register_value"],
            "quality": fact["quality"],
            "applied_multiplier": None if fact["multiplier_pending"] else fact["multiplier"],
            "multiplier_pending": bool(fact["multiplier_pending"]),
            "billable_value": _decimal_text(billable),
            "capture_id": fact["capture_id"],
            "capture_digest": fact["capture_digest"],
        }
        with self.transaction():
            cursor = self.connection.execute(
                """
                INSERT INTO issued_snapshots(
                    series_id,period,issued_at,fact_revision,register_value,quality,
                    multiplier,multiplier_pending,billable_value,basis_json,rules_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    series["id"],
                    fact["period"],
                    _now(),
                    fact["revision"],
                    fact["register_value"],
                    fact["quality"],
                    fact["multiplier"],
                    fact["multiplier_pending"],
                    _decimal_text(billable),
                    json.dumps(basis, ensure_ascii=False, sort_keys=True),
                    json.dumps(rule_snapshot(), sort_keys=True),
                ),
            )
            snapshot_id = cursor.lastrowid
        return {"snapshot_id": snapshot_id, **basis}

    def _require_series(self, reading_id: str, provider: str | None) -> sqlite3.Row:
        if provider is None:
            row = self.connection.execute(
                "SELECT * FROM meter_series WHERE reading_id=?", (reading_id,)
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM meter_series WHERE provider=? AND reading_id=?",
                (provider, reading_id),
            ).fetchone()
        if row is None:
            raise KeyError(reading_id)
        return row

    # -------------------------------------------------------------- 调整建议
    def suggest_adjustment(self, snapshot_id: int) -> dict:
        """对比签发快照与当前解释，幂等地维护一笔未决调整建议。"""
        snapshot = self.connection.execute(
            "SELECT * FROM issued_snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
        if snapshot is None:
            raise KeyError(snapshot_id)
        current = self.current_fact(snapshot["series_id"])
        open_row = self.connection.execute(
            "SELECT * FROM adjustment_suggestions WHERE snapshot_id=? AND status=?",
            (snapshot_id, PROPOSED),
        ).fetchone()

        if open_row is None and current["revision"] == snapshot["fact_revision"]:
            # 当前解释与签发依据一致：没有差异就没有建议。
            return None

        prior_accepted = self.connection.execute(
            "SELECT * FROM adjustment_suggestions WHERE snapshot_id=? AND status=?"
            " ORDER BY accepted_at DESC,id DESC LIMIT 1",
            (snapshot_id, ACCEPTED),
        ).fetchone()

        current_billable = self.billable(current)
        snapshot_billable = Decimal(snapshot["billable_value"]) if snapshot["billable_value"] is not None else None
        # 仅当当前解释仍待核时影响挂起；快照曾待核时，补录后的全额即为释放的影响。
        pending = current_billable is None
        if pending:
            billable_delta = None
        elif snapshot_billable is None:
            billable_delta = str(current_billable)
        else:
            billable_delta = str(current_billable - snapshot_billable)
        register_delta = str(Decimal(current["register_value"]) - Decimal(snapshot["register_value"]))

        prior_target = None
        incremental_delta = None
        if prior_accepted is not None:
            prior_basis = json.loads(prior_accepted["basis_json"])
            prior_target = prior_basis["current_billable"]
            if not pending and prior_target is not None:
                incremental_delta = str(current_billable - Decimal(prior_target))

        basis = {
            "snapshot_billable": snapshot["billable_value"],
            "frozen_billable_pending": snapshot_billable is None,
            "current_revision": current["revision"],
            "current_register": current["register_value"],
            "current_quality": current["quality"],
            "current_multiplier": current["multiplier"],
            "current_multiplier_pending": bool(current["multiplier_pending"]),
            "current_billable": _decimal_text(current_billable),
            "register_delta": register_delta,
            "billable_delta": billable_delta,
            "impact_pending": pending,
            "prior_suggestion_id": None if prior_accepted is None else prior_accepted["id"],
            "prior_adjusted_billable": prior_target,
            "incremental_delta_vs_prior": incremental_delta,
            "capture_id": current["capture_id"],
            "capture_digest": current["capture_digest"],
        }
        rules = rule_snapshot((RULE_REVISION, RULE_ADJUSTMENT, RULE_ADJUSTMENT_CHAIN, RULE_MULTIPLIER))

        with self.transaction():
            if open_row is not None:
                if open_row["to_revision"] == current["revision"]:
                    # 同一更正重复投递：原样返回既有建议，不生成第二笔。
                    return self._suggestion_payload(open_row)
                self.connection.execute(
                    "UPDATE adjustment_suggestions SET to_revision=?,register_delta=?,"
                    "billable_delta=?,basis_json=?,rules_json=?,prior_suggestion_id=?,updated_at=?"
                    " WHERE id=?",
                    (
                        current["revision"],
                        register_delta,
                        billable_delta,
                        json.dumps(basis, ensure_ascii=False, sort_keys=True),
                        json.dumps(rules, sort_keys=True),
                        None if prior_accepted is None else prior_accepted["id"],
                        _now(),
                        open_row["id"],
                    ),
                )
                row = self.connection.execute(
                    "SELECT * FROM adjustment_suggestions WHERE id=?", (open_row["id"],)
                ).fetchone()
            else:
                cursor = self.connection.execute(
                    """
                    INSERT INTO adjustment_suggestions(
                        snapshot_id,series_id,status,from_revision,to_revision,
                        register_delta,billable_delta,basis_json,rules_json,
                        prior_suggestion_id,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        snapshot_id,
                        snapshot["series_id"],
                        PROPOSED,
                        snapshot["fact_revision"],
                        current["revision"],
                        register_delta,
                        billable_delta,
                        json.dumps(basis, ensure_ascii=False, sort_keys=True),
                        json.dumps(rules, sort_keys=True),
                        None if prior_accepted is None else prior_accepted["id"],
                        _now(),
                        _now(),
                    ),
                )
                row = self.connection.execute(
                    "SELECT * FROM adjustment_suggestions WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
        return self._suggestion_payload(row)

    def accept_suggestion(self, suggestion_id: int, note: str | None = None) -> dict:
        """财务接受调整：登记决策台账并冻结建议，不回改签发快照。"""
        row = self.connection.execute(
            "SELECT * FROM adjustment_suggestions WHERE id=?", (suggestion_id,)
        ).fetchone()
        if row is None:
            raise KeyError(suggestion_id)
        if row["status"] == ACCEPTED:
            raise ValueError(f"suggestion {suggestion_id} already accepted and frozen")
        with self.transaction():
            decided_at = _now()
            self.connection.execute(
                "INSERT INTO adjustment_decisions(suggestion_id,decision,decided_at,note)"
                " VALUES (?,?,?,?)",
                (suggestion_id, ACCEPTED, decided_at, note),
            )
            self.connection.execute(
                "UPDATE adjustment_suggestions SET status=?,accepted_at=?,updated_at=? WHERE id=?",
                (ACCEPTED, decided_at, decided_at, suggestion_id),
            )
        return self._suggestion_payload(
            self.connection.execute(
                "SELECT * FROM adjustment_suggestions WHERE id=?", (suggestion_id,)
            ).fetchone()
        )

    def _suggestion_payload(self, row: sqlite3.Row) -> dict:
        payload = {**dict(row), "basis": json.loads(row["basis_json"]), "rules": json.loads(row["rules_json"])}
        del payload["basis_json"]
        del payload["rules_json"]
        return payload

    def list_suggestions(self, snapshot_id: int | None = None) -> list[dict]:
        if snapshot_id is None:
            rows = self.connection.execute(
                "SELECT * FROM adjustment_suggestions ORDER BY id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM adjustment_suggestions WHERE snapshot_id=? ORDER BY id",
                (snapshot_id,),
            ).fetchall()
        return [self._suggestion_payload(row) for row in rows]

    def refresh_suggestions(self) -> list[dict]:
        """对全部已签发快照刷新未决建议；无差异或重复投递均不新增建议。"""
        snapshots = self.connection.execute(
            "SELECT id FROM issued_snapshots ORDER BY id"
        ).fetchall()
        refreshed = []
        for snapshot in snapshots:
            suggestion = self.suggest_adjustment(snapshot["id"])
            if suggestion is not None:
                refreshed.append(suggestion)
        return refreshed

    # -------------------------------------------------------------- 财务查询
    def series_overview(self) -> list[dict]:
        series_rows = self.connection.execute(
            "SELECT * FROM meter_series ORDER BY id"
        ).fetchall()
        result = []
        for series in series_rows:
            entry = {
                "series_id": series["id"],
                "provider": series["provider"],
                "meter_code": series["meter_code"],
                "reading_id": series["reading_id"],
                "reused_meter_code": self.connection.execute(
                    "SELECT count(*)>1 FROM meter_series WHERE provider=? AND meter_code=?",
                    (series["provider"], series["meter_code"]),
                ).fetchone()[0]
                == 1,
                "boundary": self._boundary(series["id"]),
                "current": None,
                "issued": None,
                "suggestions": [],
            }
            current = self.current_fact(series["id"])
            if current is not None:
                current_billable = self.billable(current)
                entry["current"] = {
                    "revision": current["revision"],
                    "period": current["period"],
                    "observed_at": current["observed_at"],
                    "register_value": current["register_value"],
                    "quality": current["quality"],
                    "multiplier": current["multiplier"],
                    "multiplier_pending": bool(current["multiplier_pending"]),
                    "billable_value": _decimal_text(current_billable),
                    "impact_pending": current_billable is None,
                    "capture_digest": current["capture_digest"],
                }
            snapshot = self.connection.execute(
                "SELECT * FROM issued_snapshots WHERE series_id=?", (series["id"],)
            ).fetchone()
            if snapshot is not None:
                entry["issued"] = self._snapshot_payload(snapshot, current)
            entry["suggestions"] = self.list_suggestions(
                None if snapshot is None else snapshot["id"]
            )
            result.append(entry)
        return result

    def _boundary(self, series_id: int) -> dict:
        endpoints = self.connection.execute(
            "SELECT min(observed_at) AS first_at,max(observed_at) AS last_at,"
            " (SELECT register_value FROM reading_facts f WHERE f.series_id=s.series_id"
            "  ORDER BY observed_at,revision LIMIT 1) AS first_register,"
            " (SELECT register_value FROM reading_facts f WHERE f.series_id=s.series_id"
            "  ORDER BY observed_at DESC,revision DESC LIMIT 1) AS last_register"
            " FROM reading_facts s WHERE series_id=?",
            (series_id,),
        ).fetchone()
        if endpoints["first_at"] is None:
            return {}
        return {
            "first_reading_at": endpoints["first_at"],
            "first_register": endpoints["first_register"],
            "last_reading_at": endpoints["last_at"],
            "last_register": endpoints["last_register"],
        }

    def _snapshot_payload(self, snapshot: sqlite3.Row, current: sqlite3.Row | None) -> dict:
        payload = {
            "snapshot_id": snapshot["id"],
            "period": snapshot["period"],
            "issued_at": snapshot["issued_at"],
            "frozen_revision": snapshot["fact_revision"],
            "frozen_register_value": snapshot["register_value"],
            "frozen_quality": snapshot["quality"],
            "frozen_multiplier": snapshot["multiplier"],
            "multiplier_pending_at_issue": bool(snapshot["multiplier_pending"]),
            "frozen_billable_value": snapshot["billable_value"],
            "basis": json.loads(snapshot["basis_json"]),
            "rules": json.loads(snapshot["rules_json"]),
        }
        if current is not None:
            current_billable = self.billable(current)
            frozen_billable = (
                Decimal(snapshot["billable_value"]) if snapshot["billable_value"] is not None else None
            )
            impact_pending = current_billable is None
            if impact_pending:
                delta = None
            elif frozen_billable is None:
                delta = str(current_billable)
            else:
                delta = str(current_billable - frozen_billable)
            payload["difference_vs_current"] = {
                "current_revision": current["revision"],
                "register_delta": str(
                    Decimal(current["register_value"]) - Decimal(snapshot["register_value"])
                ),
                "billable_delta": delta,
                "impact_pending": impact_pending,
                "frozen_billable_pending": frozen_billable is None,
                "quality_changed": current["quality"] != snapshot["quality"],
            }
        return payload

    def difference_detail(self, snapshot_id: int) -> dict:
        """从一笔差异完整回溯：冻结依据、当前事实、适用区间、报文摘要、规则、建议链。"""
        snapshot = self.connection.execute(
            "SELECT * FROM issued_snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
        if snapshot is None:
            raise KeyError(snapshot_id)
        series = self.connection.execute(
            "SELECT * FROM meter_series WHERE id=?", (snapshot["series_id"],)
        ).fetchone()
        current = self.current_fact(series["id"])
        interval = self._interval_at(series["id"], current["observed_at"])
        facts = self.connection.execute(
            "SELECT revision,observed_at,period,register_value,quality,multiplier,"
            "multiplier_pending,capture_id,capture_digest,ingested_at FROM reading_facts"
            " WHERE series_id=? ORDER BY revision",
            (series["id"],),
        ).fetchall()
        rule_ids = set(json.loads(snapshot["rules_json"]))
        for suggestion in self.list_suggestions(snapshot_id):
            rule_ids.update(suggestion["rules"])
        return {
            "series": {**dict(series)},
            "issued_snapshot": self._snapshot_payload(snapshot, current),
            "current_fact": None
            if current is None
            else {
                "revision": current["revision"],
                "observed_at": current["observed_at"],
                "period": current["period"],
                "register_value": current["register_value"],
                "quality": current["quality"],
                "multiplier": current["multiplier"],
                "multiplier_pending": bool(current["multiplier_pending"]),
                "billable_value": _decimal_text(self.billable(current)),
                "capture_id": current["capture_id"],
                "capture_digest": current["capture_digest"],
                "raw_record": json.loads(current["raw_record"]),
            },
            "applied_multiplier_interval": None
            if interval is None
            else {
                "effective_from": interval["effective_from"],
                "effective_to": interval["effective_to"],
                "multiplier": interval["multiplier"],
                "pending": bool(interval["pending"]),
            },
            "fact_chain": [dict(row) for row in facts],
            "suggestion_chain": self.list_suggestions(snapshot_id),
            "applicable_rules": {rule_id: RULE_CATALOG[rule_id] for rule_id in sorted(rule_ids)},
        }
