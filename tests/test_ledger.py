import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from energy_meter.finance import (
    finance_comparison,
    meter_series,
    pending_impacts,
    trace_reading,
    trace_suggestion,
)
from energy_meter.ingest import normalize_record
from energy_meter.ledger import Ledger, ReadingConflict, SnapshotError
from energy_meter.rules import (
    IDENTITY_PENDING,
    IDENTITY_RESOLVED,
    MULTIPLIER_KNOWN,
    MULTIPLIER_PENDING,
    RuleBook,
    RuleConflict,
)

RULES_PATH = Path(__file__).parents[1] / "fixtures" / "ledger-rules.json"


def rules():
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def rec(meter, reading, revision, *, value, quality="measured", multiplier="2",
        at="2026-10-31T23:00:00Z", period="2026-10", provider="grid-a"):
    return {
        "provider": provider, "meter": meter, "reading": reading,
        "revision": revision, "at": at, "value": value, "quality": quality,
        "multiplier": multiplier, "period": period,
    }


class RuleBookTests(unittest.TestCase):
    def test_resolves_install_and_factor_by_interval(self):
        book = RuleBook.create(rules())
        from datetime import datetime
        at = datetime.fromisoformat("2026-10-31T23:00:00+00:00")
        self.assertEqual(book.resolve_install("grid-a", "M7-old", at),
                         "grid-a/M7-old@2026H2")
        self.assertEqual(book.resolve_factor("grid-a/M7-old@2026H2", at), Decimal("2"))

    def test_reused_label_resolves_to_distinct_installs(self):
        book = RuleBook.create(rules())
        from datetime import datetime
        before = datetime.fromisoformat("2026-08-15T00:00:00+00:00")
        after = datetime.fromisoformat("2026-09-15T00:00:00+00:00")
        self.assertEqual(book.resolve_install("grid-a", "M3", before),
                         "grid-a/M3@2025")
        self.assertEqual(book.resolve_install("grid-a", "M3", after),
                         "grid-a/M3@2026-reuse")

    def test_unknown_factor_is_none_never_one(self):
        book = RuleBook.create(rules())
        from datetime import datetime
        at = datetime.fromisoformat("2026-11-05T00:00:00+00:00")
        install = book.resolve_install("grid-a", "M7-new", at)
        self.assertEqual(install, "grid-a/M7-new@2026H2")
        self.assertIsNone(book.resolve_factor(install, at))

    def test_overlapping_intervals_are_rejected(self):
        bad = rules()
        bad["meters"].append({
            "provider": "grid-a", "meter": "M7-old",
            "install": "wrong", "valid_from": "2026-10-01T00:00:00Z",
        })
        with self.assertRaises(RuleConflict):
            RuleBook.create(bad)

    def test_unknown_install_multiplier_is_rejected(self):
        bad = rules()
        bad["multipliers"].append({
            "install": "ghost", "factor": "9",
            "valid_from": "2026-01-01T00:00:00Z",
        })
        with self.assertRaises(RuleConflict):
            RuleBook.create(bad)


class NormalizationTests(unittest.TestCase):
    def test_keeps_identity_quality_and_null_multiplier(self):
        message = normalize_record(rec("M7-new", "R11", 1, value="8", multiplier=None,
                                       at="2026-11-01T00:00:00Z", period="2026-11"))
        self.assertEqual(message.meter_label, "M7-new")
        self.assertEqual(message.quality, "measured")
        self.assertIsNone(message.declared_factor)
        self.assertEqual(message.register_value, Decimal("8"))

    def test_rejects_bad_quality_revision_and_cross_month_period(self):
        with self.assertRaisesRegex(ValueError, "quality"):
            normalize_record(rec("M7-old", "R10", 1, value="1", quality="guessed"))
        with self.assertRaisesRegex(ValueError, "revision"):
            normalize_record(rec("M7-old", "R10", 0, value="1"))
        with self.assertRaisesRegex(ValueError, "period"):
            normalize_record(rec("M7-old", "R10", 1, value="1", period="2026-09"))
        with self.assertRaisesRegex(ValueError, "multiplier"):
            normalize_record(rec("M7-old", "R10", 1, value="1", multiplier="0"))


class LedgerIngestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temporary.name) / "ledger.sqlite3")
        self.ledger.publish_rules(rules())

    def tearDown(self):
        self.ledger.close()
        self.temporary.cleanup()

    def test_out_of_order_revisions_converge_on_latest(self):
        self.ledger.ingest_records([rec("M7-old", "R10", 2, value="1012")])
        self.ledger.ingest_records([rec("M7-old", "R10", 1, value="1000",
                                        quality="estimated")])
        current = self._current("R10")
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["register_value"], "1012")
        self.assertEqual(current["quality"], "measured")
        self.assertEqual(current["identity_status"], IDENTITY_RESOLVED)
        self.assertEqual(current["multiplier_status"], MULTIPLIER_KNOWN)
        self.assertEqual(current["factor"], "2")
        self.assertEqual(current["gross_value"], "2024")
        versions = self.ledger.interpretation_versions("R10")
        self.assertEqual(len(versions), 1)
        # 迟到的旧修订不能把当前解释拉回去。
        self.ledger.ingest_records([rec("M7-old", "R10", 1, value="1000",
                                        quality="estimated")])
        self.assertEqual(self._current("R10")["revision"], 2)

    def test_redelivery_is_idempotent_and_keeps_message_audit(self):
        first = self.ledger.ingest_records(
            [rec("M7-old", "R10", 1, value="1000", quality="estimated")])
        second = self.ledger.ingest_records(
            [rec("M7-old", "R10", 1, value="1000", quality="estimated")])
        self.assertEqual((first["ingested"], first["duplicates"]), (1, 0))
        self.assertEqual((second["ingested"], second["duplicates"]), (0, 1))
        messages = self.ledger.connection.execute(
            "SELECT count(*) AS n FROM reading_messages").fetchone()["n"]
        self.assertEqual(messages, 1)

    def test_same_revision_different_payload_is_a_conflict(self):
        self.ledger.ingest_records([rec("M7-old", "R10", 1, value="1000")])
        with self.assertRaises(ReadingConflict):
            self.ledger.ingest_records([rec("M7-old", "R10", 1, value="1001")])
        self.assertEqual(self._current("R10")["register_value"], "1000")

    def test_unknown_multiplier_stays_pending_without_default_one(self):
        self.ledger.ingest_records([
            rec("M7-new", "R11", 1, value="8", multiplier=None,
                at="2026-11-01T00:00:00Z", period="2026-11"),
        ])
        current = self._current("R11")
        self.assertEqual(current["multiplier_status"], MULTIPLIER_PENDING)
        self.assertIsNone(current["factor"])
        self.assertIsNone(current["gross_value"])
        self.assertIn("倍率", current["multiplier_reason"])

    def test_declared_factor_mismatch_is_pending(self):
        self.ledger.ingest_records([
            rec("M7-old", "R10", 1, value="1000", multiplier="7"),
        ])
        current = self._current("R10")
        self.assertEqual(current["multiplier_status"], MULTIPLIER_PENDING)
        self.assertIsNone(current["factor"])
        self.assertIn("不一致", current["multiplier_reason"])

    def test_unknown_identity_is_pending(self):
        self.ledger.ingest_records([
            rec("M99-ghost", "R20", 1, value="5", multiplier="2"),
        ])
        current = self._current("R20")
        self.assertEqual(current["identity_status"], IDENTITY_PENDING)
        self.assertEqual(current["multiplier_status"], MULTIPLIER_PENDING)
        self.assertIsNone(current["gross_value"])

    def test_revision_chain_appends_interpretation_versions(self):
        for revision, value in ((1, "1000"), (2, "1012"), (3, "1020")):
            self.ledger.ingest_records([
                rec("M7-old", "R10", revision, value=value,
                    quality="estimated" if revision == 1 else "measured"),
            ])
        versions = self.ledger.interpretation_versions("R10")
        self.assertEqual([row["revision"] for row in versions], [1, 2, 3])
        self.assertEqual(len({row["interpretation_digest"] for row in versions}), 3)
        current = self._current("R10")
        self.assertEqual(current["revision"], 3)
        self.assertEqual(current["version_id"], versions[-1]["version_id"])

    def _current(self, reading_key):
        rows = self.ledger.interpretations()
        return next(row for row in rows if row["reading_key"] == reading_key)


class MeterChangeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temporary.name) / "ledger.sqlite3")
        self.ledger.publish_rules(rules())

    def tearDown(self):
        self.ledger.close()
        self.temporary.cleanup()

    def test_meter_change_keeps_tail_and_head_on_separate_series(self):
        self.ledger.ingest_records([
            rec("M7-old", "R10", 1, value="1012",
                at="2026-10-31T23:00:00Z", period="2026-10"),
            rec("M7-new", "R11", 1, value="8", multiplier=None,
                at="2026-11-01T00:00:00Z", period="2026-11"),
        ])
        view = meter_series(self.ledger, "grid-a", "M7-old")
        self.assertEqual(len(view["series"]), 1)
        self.assertEqual(view["series"][0]["last_reading"], "1012")
        new_view = meter_series(self.ledger, "grid-a", "M7-new")
        self.assertEqual(new_view["series"][0]["first_reading"], "8")
        self.assertEqual(new_view["series"][0]["join_allowed_with"], [])

    def test_reused_device_number_never_joins_one_curve(self):
        self.ledger.ingest_records([
            rec("M3", "A1", 1, value="500", multiplier="1.5",
                at="2026-08-31T23:00:00Z", period="2026-08"),
            rec("M3", "A2", 1, value="3", multiplier="3",
                at="2026-09-30T23:00:00Z", period="2026-09"),
        ])
        view = meter_series(self.ledger, "grid-a", "M3")
        self.assertTrue(view["reused_label"])
        self.assertEqual(len(view["series"]), 2)
        install_ids = [item["install_id"] for item in view["series"]]
        self.assertEqual(install_ids, ["grid-a/M3@2025", "grid-a/M3@2026-reuse"])
        boundary = view["boundaries"][0]
        self.assertEqual(boundary["old_meter_tail"], "500")
        self.assertEqual(boundary["new_meter_head"], "3")
        self.assertNotEqual(boundary["from_install"], boundary["to_install"])


class SnapshotAndAdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temporary.name) / "ledger.sqlite3")
        self.ledger.publish_rules(rules())
        # 账期 2026-10：估算修订1先到，按它签发月账。
        self.ledger.ingest_records([
            rec("M7-old", "R10", 1, value="1000", quality="estimated"),
        ])
        self.snapshot_id = self.ledger.issue_snapshot(
            "2026-10", note="十月月账")["snapshot_id"]

    def tearDown(self):
        self.ledger.close()
        self.temporary.cleanup()

    def test_issued_snapshot_and_readings_are_immutable(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute(
                "UPDATE snapshots SET note='x' WHERE snapshot_id=?",
                (self.snapshot_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute(
                "UPDATE snapshot_readings SET register_value='1' WHERE snapshot_id=?",
                (self.snapshot_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute(
                "DELETE FROM snapshot_readings WHERE snapshot_id=?",
                (self.snapshot_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute(
                "DELETE FROM snapshots WHERE snapshot_id=?", (self.snapshot_id,))

    def test_correction_updates_current_view_but_not_issued_snapshot(self):
        self.ledger.ingest_records([rec("M7-old", "R10", 2, value="1012")])
        self.ledger.ingest_records([rec("M7-old", "R10", 3, value="1020")])
        issued = self.ledger.connection.execute(
            "SELECT * FROM snapshot_readings WHERE snapshot_id=?",
            (self.snapshot_id,)).fetchone()
        self.assertEqual(issued["revision"], 1)
        self.assertEqual(issued["register_value"], "1000")
        current = next(row for row in self.ledger.interpretations("2026-10")
                       if row["reading_key"] == "R10")
        self.assertEqual(current["revision"], 3)
        self.assertEqual(current["register_value"], "1020")

    def test_adjustment_is_created_once_and_redelivery_does_not_duplicate(self):
        self.ledger.ingest_records([rec("M7-old", "R10", 2, value="1012")])
        first = self.ledger.generate_adjustments(self.snapshot_id)
        self.assertEqual(len(first["created"]), 1)
        suggestion = first["created"][0]
        self.assertEqual(suggestion["register_delta"], "12")
        self.assertEqual(suggestion["gross_delta"], "24")
        again = self.ledger.generate_adjustments(self.snapshot_id)
        self.assertEqual(again["created"], [])
        self.assertEqual(again["deduplicated"], 1)
        self.assertEqual(len(self.ledger.suggestions(self.snapshot_id)), 1)

    def test_accepted_adjustment_then_new_correction_shows_chain(self):
        self.ledger.ingest_records([rec("M7-old", "R10", 2, value="1012")])
        first = self.ledger.generate_adjustments(self.snapshot_id)["created"][0]
        accepted = self.ledger.accept_adjustment(
            first["suggestion_id"], note="财务接受修订2调整")
        self.assertEqual(accepted["status"], "accepted")
        with self.assertRaisesRegex(SnapshotError, "already accepted"):
            self.ledger.accept_adjustment(first["suggestion_id"])
        # 财务接受后再来更正（修订3）。
        self.ledger.ingest_records([rec("M7-old", "R10", 3, value="1020")])
        second = self.ledger.generate_adjustments(self.snapshot_id)["created"]
        self.assertEqual(len(second), 1)
        follow_up = second[0]
        self.assertEqual(follow_up["prior_suggestion_id"], first["suggestion_id"])
        # 对签发口径：1020-1000=20；对前次调整后口径：1020-1012=8。
        self.assertEqual(follow_up["register_delta"], "20")
        self.assertEqual(follow_up["delta_vs_prior_register"], "8")
        self.assertEqual(follow_up["gross_delta"], "40")
        self.assertEqual(follow_up["delta_vs_prior_gross"], "16")
        self.assertIn("读数修订 1→3", follow_up["change_summary"])
        self.assertIn("质量 estimated→measured", follow_up["change_summary"])
        chain = self.ledger.suggestions(self.snapshot_id)
        self.assertEqual([row["status"] for row in chain], ["accepted", "proposed"])

    def test_no_adjustment_when_interpretation_unchanged(self):
        result = self.ledger.generate_adjustments(self.snapshot_id)
        self.assertEqual(result["created"], [])
        self.assertEqual(result["unchanged"], 1)

    def test_snapshot_issue_is_idempotent_for_same_state(self):
        again = self.ledger.issue_snapshot("2026-10", note="重复签发请求")
        self.assertTrue(again["reused"])
        self.assertEqual(again["snapshot_id"], self.snapshot_id)


class FinanceViewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temporary.name) / "ledger.sqlite3")
        self.ledger.publish_rules(rules())
        self.ledger.ingest_records([
            rec("M7-old", "R10", 1, value="1000", quality="estimated"),
            rec("M7-new", "R11", 1, value="8", multiplier=None,
                at="2026-11-01T00:00:00Z", period="2026-11"),
        ])
        self.snapshot_id = self.ledger.issue_snapshot("2026-10")["snapshot_id"]
        self.ledger.ingest_records([rec("M7-old", "R10", 2, value="1012")])
        self.ledger.generate_adjustments(self.snapshot_id)

    def tearDown(self):
        self.ledger.close()
        self.temporary.cleanup()

    def test_comparison_shows_issued_current_impact_side_by_side(self):
        view = finance_comparison(self.ledger, self.snapshot_id)
        row = next(item for item in view["readings"]
                    if item["reading_key"] == "R10")
        self.assertEqual(row["issued"]["register_value"], "1000")
        self.assertEqual(row["issued"]["quality"], "estimated")
        self.assertEqual(row["current"]["register_value"], "1012")
        self.assertEqual(row["impact"]["register_delta"], "12")
        self.assertTrue(row["impact"]["changed"])
        self.assertEqual(row["adjustment"]["status"], "proposed")
        self.assertEqual(view["summary"]["changed"], 1)

    def test_pending_impact_blocks_amount_and_explains_reason(self):
        pending = pending_impacts(self.ledger)
        self.assertEqual(pending["pending_count"], 1)
        item = pending["readings"][0]
        self.assertEqual(item["reading_key"], "R11")
        self.assertTrue(item["blocked_amount"])
        self.assertIsNone(item["gross_value"])
        self.assertIn("倍率", item["reason"])

    def test_trace_returns_every_supplier_message_rule_and_chain(self):
        traced = trace_reading(self.ledger, "R10")
        self.assertEqual([m["revision"] for m in traced["supplier_messages"]], [1, 2])
        message = traced["supplier_messages"][0]
        self.assertEqual(message["payload"]["value"], "1000")
        self.assertIn("default@1", traced["applicable_rules"])
        rule = traced["applicable_rules"]["default@1"]
        self.assertEqual(rule["content"]["multipliers"][0]["factor"], "2")
        self.assertEqual(len(traced["interpretation_versions"]), 2)
        self.assertEqual(traced["issued_snapshots"][0]["snapshot_id"], self.snapshot_id)
        self.assertEqual(len(traced["adjustment_chain"]), 1)
        # 溯源报文的摘要与台账消息摘要一致。
        self.assertEqual(
            message["payload_digest"],
            self.ledger.message(message["message_id"])["payload_digest"],
        )

    def test_trace_suggestion_links_difference_to_basis(self):
        suggestion_id = self.ledger.suggestions(self.snapshot_id)[0]["suggestion_id"]
        result = trace_suggestion(self.ledger, suggestion_id)
        self.assertEqual(result["suggestion"]["reading_key"], "R10")
        revisions = [m["revision"]
                     for m in result["trace"]["supplier_messages"]]
        self.assertEqual(revisions, [1, 2])

    def test_persists_across_reopen_without_losing_basis(self):
        self.ledger.close()
        reopened = Ledger(Path(self.temporary.name) / "ledger.sqlite3")
        try:
            self.assertEqual(len(reopened.interpretations()), 2)
            view = finance_comparison(reopened, self.snapshot_id)
            self.assertEqual(view["rule_basis"]["version"], 1)
            self.assertEqual(
                view["readings"][0]["issued"]["register_value"], "1000")
            self.assertEqual(
                view["readings"][0]["current"]["register_value"], "1012")
            self.assertEqual(len(reopened.suggestions(self.snapshot_id)), 1)
        finally:
            reopened.close()
        # 恢复 setUp/tearDown 约定的打开句柄。
        self.ledger = Ledger(Path(self.temporary.name) / "ledger.sqlite3")

    def test_rule_republish_reinterprets_without_touching_snapshot(self):
        new_rules = rules()
        new_rules["multipliers"].append({
            "install": "grid-a/M7-new@2026H2",
            "factor": "4",
            "valid_from": "2026-11-01T00:00:00Z",
        })
        book = self.ledger.publish_rules(new_rules)
        self.assertEqual(book.version, 2)
        self.ledger.reinterpret_all(book)
        r11 = next(row for row in self.ledger.interpretations("2026-11")
                   if row["reading_key"] == "R11")
        self.assertEqual(r11["multiplier_status"], MULTIPLIER_KNOWN)
        self.assertEqual(r11["factor"], "4")
        self.assertEqual(r11["gross_value"], "32")
        issued = self.ledger.connection.execute(
            "SELECT * FROM snapshot_readings WHERE snapshot_id=?",
            (self.snapshot_id,)).fetchone()
        self.assertEqual(issued["rule_basis_id"], "default@1")
        # 规则换版使 R10 解释摘要变化，应产生一笔新的调整建议。
        result = self.ledger.generate_adjustments(self.snapshot_id)
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(result["created"][0]["reading_key"], "R10")


if __name__ == "__main__":
    unittest.main()
