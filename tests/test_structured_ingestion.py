import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from energy_meter.archive import Archive
from energy_meter.domain import (
    RULE_ADJUSTMENT,
    RULE_ADJUSTMENT_CHAIN,
    RULE_MULTIPLIER,
    RULE_REVISION,
    ReadingRecordError,
    normalize_reading,
)
from energy_meter.intake import ingest_latest
from energy_meter.store import ACCEPTED, IngestStore, PROPOSED


def record(meter, reading, revision, value, quality="measured", multiplier="2",
           at="2026-10-31T23:00:00Z", period="2026-10", provider="grid-a"):
    return {
        "provider": provider,
        "meter": meter,
        "reading": reading,
        "revision": revision,
        "at": at,
        "value": value,
        "quality": quality,
        "multiplier": multiplier,
        "period": period,
    }


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = IngestStore(Path(self.temporary.name) / "readings.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def ingest(self, *records, capture=None):
        return [self.store.ingest_record(item, capture) for item in records]


class NormalizationTests(unittest.TestCase):
    def test_unknown_multiplier_stays_pending_never_one(self):
        normalized = normalize_reading(record("M7-new", "R11", 1, "8", multiplier=None))
        self.assertIsNone(normalized.multiplier)

    def test_quality_must_be_measured_or_estimated(self):
        bad = record("M", "R", 1, "1", quality="guessed")
        with self.assertRaisesRegex(ReadingRecordError, "quality"):
            normalize_reading(bad)

    def test_rejects_bad_identity_revision_period_and_multiplier(self):
        with self.assertRaises(ReadingRecordError):
            normalize_reading(record("M", "R", 0, "1"))
        with self.assertRaises(ReadingRecordError):
            normalize_reading(record("M", "R", 1, "1", multiplier="0"))
        bad_period = record("M", "R", 1, "1", period="2026/10")
        with self.assertRaisesRegex(ReadingRecordError, "YYYY-MM"):
            normalize_reading(bad_period)
        bad_time = record("M", "R", 1, "1", at="2026-10-31 23:00:00")
        with self.assertRaisesRegex(ReadingRecordError, "timezone"):
            normalize_reading(bad_time)


class SeriesAndCurveTests(StoreTestCase):
    def test_meter_change_keeps_two_series_with_own_boundaries(self):
        self.ingest(
            record("M7-old", "R10", 1, "1000", at="2026-10-31T23:00:00Z"),
            record("M7-new", "R11", 1, "8", at="2026-11-01T00:00:00Z", period="2026-11", multiplier=None),
        )
        overview = self.store.series_overview()
        self.assertEqual([item["reading_id"] for item in overview], ["R10", "R11"])
        old, new = overview
        self.assertEqual(old["boundary"]["last_register"], "1000")
        self.assertEqual(new["boundary"]["first_register"], "8")
        # 两条序列各自独立，没有拼成一条累计曲线。
        self.assertNotEqual(old["series_id"], new["series_id"])

    def test_reused_meter_code_opens_separate_series_not_joined_curve(self):
        self.ingest(
            record("M9", "R20", 1, "500", at="2026-09-30T23:00:00Z", period="2026-09"),
            record("M9", "R30", 1, "3", at="2026-10-31T23:00:00Z"),
        )
        overview = self.store.series_overview()
        self.assertEqual(len(overview), 2)
        self.assertTrue(all(item["reused_meter_code"] for item in overview))
        self.assertEqual(overview[0]["boundary"]["last_register"], "500")
        self.assertEqual(overview[1]["boundary"]["first_register"], "3")


class CurrentInterpretationTests(StoreTestCase):
    def test_out_of_order_revisions_current_takes_highest(self):
        self.ingest(record("M7-old", "R10", 2, "1012", quality="measured"))
        self.ingest(record("M7-old", "R10", 1, "1000", quality="estimated"))
        current = self.store.current_fact(1)
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["quality"], "measured")
        # 乱序补传不丢事实，修订链完整保留。
        revisions = [
            row["revision"]
            for row in self.store.connection.execute(
                "SELECT revision FROM reading_facts WHERE series_id=1 ORDER BY revision"
            )
        ]
        self.assertEqual(revisions, [1, 2])

    def test_duplicate_delivery_is_idempotent_at_fact_level(self):
        first = self.ingest(record("M", "R10", 1, "1000"))
        second = self.ingest(record("M", "R10", 1, "1000"))
        self.assertFalse(first[0]["duplicated"])
        self.assertTrue(second[0]["duplicated"])
        count = self.store.connection.execute("SELECT count(*) FROM reading_facts").fetchone()[0]
        self.assertEqual(count, 1)

    def test_multiplier_intervals_apply_by_observation_time(self):
        self.ingest(
            record("M", "R40", 1, "100", multiplier="2", at="2026-10-15T00:00:00Z"),
            record("M", "R40", 2, "120", multiplier="3", at="2026-11-15T00:00:00Z", period="2026-11"),
        )
        first = self.store.connection.execute(
            "SELECT * FROM reading_facts WHERE revision=1"
        ).fetchone()
        second = self.store.connection.execute(
            "SELECT * FROM reading_facts WHERE revision=2"
        ).fetchone()
        self.assertEqual(self.store.billable(first), Decimal("200"))
        self.assertEqual(self.store.billable(second), Decimal("360"))
        intervals = self.store.connection.execute(
            "SELECT multiplier,pending FROM multiplier_intervals ORDER BY effective_from"
        ).fetchall()
        self.assertEqual([(row["multiplier"], bool(row["pending"])) for row in intervals], [("2", False), ("3", False)])

    def test_pending_multiplier_blocks_billable_value(self):
        self.ingest(record("M", "R11", 1, "8", multiplier=None))
        current = self.store.current_fact(1)
        self.assertTrue(current["multiplier_pending"])
        self.assertIsNone(self.store.billable(current))


class SnapshotAndAdjustmentTests(StoreTestCase):
    def _issued_with_correction(self):
        self.ingest(record("M7-old", "R10", 1, "1000", quality="estimated"))
        snapshot = self.store.issue_snapshot("R10", "grid-a")
        self.ingest(record("M7-old", "R10", 2, "1012", quality="measured"))
        return snapshot

    def test_snapshot_freezes_even_when_current_interpretation_changes(self):
        snapshot = self._issued_with_correction()
        stored = self.store.connection.execute(
            "SELECT * FROM issued_snapshots WHERE id=?", (snapshot["snapshot_id"],)
        ).fetchone()
        self.assertEqual(stored["fact_revision"], 1)
        self.assertEqual(stored["register_value"], "1000")
        self.assertEqual(stored["quality"], "estimated")
        self.assertEqual(stored["billable_value"], "2000")
        self.assertIn(RULE_MULTIPLIER, json.loads(stored["rules_json"]))
        current = self.store.current_fact(1)
        self.assertEqual(current["revision"], 2)
        with self.assertRaisesRegex(ValueError, "frozen"):
            self.store.issue_snapshot("R10", "grid-a")

    def test_snapshot_and_facts_persist_across_reopen(self):
        self.ingest(record("M", "R10", 1, "1000"))
        snapshot = self.store.issue_snapshot("R10", "grid-a")
        path = self.store.path
        self.store.close()
        reopened = IngestStore(path)
        try:
            row = reopened.connection.execute(
                "SELECT * FROM issued_snapshots WHERE id=?", (snapshot["snapshot_id"],)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["register_value"], "1000")
            self.assertEqual(reopened.connection.execute(
                "SELECT count(*) FROM meter_series").fetchone()[0], 1)
        finally:
            reopened.close()

    def test_adjustment_suggestion_carries_documented_delta_and_basis(self):
        snapshot = self._issued_with_correction()
        suggestion = self.store.suggest_adjustment(snapshot["snapshot_id"])
        self.assertEqual(suggestion["status"], PROPOSED)
        self.assertEqual((suggestion["from_revision"], suggestion["to_revision"]), (1, 2))
        self.assertEqual(suggestion["register_delta"], "12")
        self.assertEqual(suggestion["billable_delta"], "24")
        self.assertFalse(suggestion["basis"]["impact_pending"])

    def test_repeated_delivery_does_not_create_second_suggestion(self):
        snapshot = self._issued_with_correction()
        first = self.store.suggest_adjustment(snapshot["snapshot_id"])
        # 再次投递相同的 rev2 报文（重复补传）。
        self.ingest(record("M7-old", "R10", 2, "1012", quality="measured"))
        again = self.store.suggest_adjustment(snapshot["snapshot_id"])
        self.assertEqual(again["id"], first["id"])
        rows = self.store.connection.execute("SELECT count(*) FROM adjustment_suggestions").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_no_difference_means_no_suggestion(self):
        self.ingest(record("M", "R10", 1, "1000"))
        snapshot = self.store.issue_snapshot("R10", "grid-a")
        self.assertIsNone(self.store.suggest_adjustment(snapshot["snapshot_id"]))

    def test_accepted_adjustment_is_frozen_and_chain_links_next_correction(self):
        snapshot = self._issued_with_correction()
        first = self.store.suggest_adjustment(snapshot["snapshot_id"])
        accepted = self.store.accept_suggestion(first["id"], note="财务确认十月更正")
        self.assertEqual(accepted["status"], ACCEPTED)
        with self.assertRaisesRegex(ValueError, "frozen"):
            self.store.accept_suggestion(first["id"])
        # 接受后又来一笔更正（rev3）。
        self.ingest(record("M7-old", "R10", 3, "1030", quality="measured"))
        second = self.store.suggest_adjustment(snapshot["snapshot_id"])
        self.assertIsNotNone(second["id"])
        self.assertEqual(second["prior_suggestion_id"], first["id"])
        # 相对前次调整后口径（1012*2=2024）的新差额为 (1030-1012)*2=36。
        self.assertEqual(second["basis"]["incremental_delta_vs_prior"], "36")
        self.assertEqual(second["basis"]["prior_adjusted_billable"], "2024")
        self.assertEqual(second["billable_delta"], "60")
        self.assertIn(RULE_ADJUSTMENT_CHAIN, second["rules"])
        decisions = self.store.connection.execute("SELECT * FROM adjustment_decisions").fetchall()
        self.assertEqual(len(decisions), 1)

    def test_pending_multiplier_suspends_impact_until_verified(self):
        self.ingest(record("M", "R11", 1, "8", multiplier=None))
        snapshot = self.store.issue_snapshot("R11", "grid-a")
        self.assertIsNone(snapshot["billable_value"])
        self.ingest(record("M", "R11", 2, "9", multiplier=None))
        suggestion = self.store.suggest_adjustment(snapshot["snapshot_id"])
        self.assertTrue(suggestion["basis"]["impact_pending"])
        self.assertIsNone(suggestion["billable_delta"])
        # 倍率补录后重新计算，影响解除挂起；快照曾待核，全额 9*4=36 即释放的影响。
        self.ingest(record("M", "R11", 3, "9", multiplier="4"))
        resolved = self.store.suggest_adjustment(snapshot["snapshot_id"])
        self.assertFalse(resolved["basis"]["impact_pending"])
        self.assertTrue(resolved["basis"]["frozen_billable_pending"])
        self.assertEqual(resolved["billable_delta"], "36")


class FinanceViewTests(StoreTestCase):
    def test_status_places_issued_current_and_pending_side_by_side(self):
        self.ingest(
            record("M7-old", "R10", 1, "1000", quality="estimated"),
            record("M7-new", "R11", 1, "8", multiplier=None, at="2026-11-01T00:00:00Z", period="2026-11"),
        )
        self.store.issue_snapshot("R10", "grid-a")
        self.ingest(record("M7-old", "R10", 2, "1012", quality="measured"))
        self.store.refresh_suggestions()
        overview = self.store.series_overview()
        r10 = next(item for item in overview if item["reading_id"] == "R10")
        self.assertEqual(r10["issued"]["frozen_register_value"], "1000")
        self.assertEqual(r10["current"]["register_value"], "1012")
        self.assertEqual(r10["issued"]["difference_vs_current"]["billable_delta"], "24")
        self.assertTrue(r10["issued"]["difference_vs_current"]["quality_changed"])
        self.assertEqual(len(r10["suggestions"]), 1)
        r11 = next(item for item in overview if item["reading_id"] == "R11")
        self.assertTrue(r11["current"]["impact_pending"])
        self.assertIsNone(r11["current"]["billable_value"])

    def test_difference_traces_back_to_archive_message_and_rules(self):
        capture = {"capture_id": 7, "digest": "abc123", "source": "local-source",
                   "url": "http://origin/feed", "received_at": "2026-11-02T01:02:03.000Z"}
        self.ingest(record("M7-old", "R10", 1, "1000", quality="estimated"), capture=capture)
        snapshot = self.store.issue_snapshot("R10", "grid-a")
        self.ingest(record("M7-old", "R10", 2, "1012", quality="measured"), capture=capture)
        detail = self.store.difference_detail(snapshot["snapshot_id"])
        self.assertEqual(detail["current_fact"]["capture_digest"], "abc123")
        self.assertEqual(detail["current_fact"]["capture_id"], 7)
        self.assertEqual(detail["current_fact"]["raw_record"]["reading"], "R10")
        self.assertEqual(detail["applied_multiplier_interval"]["multiplier"], "2")
        self.assertEqual(len(detail["fact_chain"]), 2)
        self.assertIn(RULE_REVISION, detail["applicable_rules"])
        self.assertIn(RULE_ADJUSTMENT, detail["applicable_rules"])


class ArchiveBridgeTests(unittest.TestCase):
    def test_ingest_from_archive_links_facts_to_capture_digest(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        archive = Archive(base / "raw.sqlite3")
        body = json.dumps({"items": [record("M7-old", "R10", 1, "1000")]}).encode()
        capture_id = archive.append("local-source", "http://origin/feed", 200,
                                    {"content-type": "application/json"}, body)
        store = IngestStore(base / "readings.sqlite3")
        self.addCleanup(store.close)
        self.addCleanup(archive.close)
        result = ingest_latest(store, archive)
        self.assertEqual(result["capture_id"], capture_id)
        fact = store.connection.execute("SELECT * FROM reading_facts").fetchone()
        self.assertEqual(fact["capture_id"], capture_id)
        self.assertEqual(fact["capture_digest"], archive.captures()[0]["digest"])
        # 原始归档不被结构化层改写。
        self.assertEqual(len(archive.captures()), 1)

    def test_invalid_record_is_rejected_without_poisoning_others(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        archive = Archive(base / "raw.sqlite3")
        good = record("M", "R10", 1, "1000")
        bad = record("M", "R11", 1, "1", quality="guessed")
        body = json.dumps({"items": [good, bad]}).encode()
        archive.append("local-source", "http://origin/feed", 200,
                       {"content-type": "application/json"}, body)
        store = IngestStore(base / "readings.sqlite3")
        self.addCleanup(store.close)
        self.addCleanup(archive.close)
        result = ingest_latest(store, archive)
        self.assertEqual(len(result["ingested"]), 1)
        self.assertEqual(len(result["rejected"]), 1)
        self.assertIn("quality", result["rejected"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
