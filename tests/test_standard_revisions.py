import sqlite3

from fastapi.testclient import TestClient

from carbon_trace.api import create_app
from carbon_trace.service import Service
from common import ASSAY, LINE, LOT, ORDER, PLANT, SPEC, Scenario


def amendment(**changes):
    payload = {"amendment_id": "AM1", "spec_id": "S2", "valid_from": "2026-02-01",
               "sulfur_min": "0", "sulfur_max": "0.5", "ash_max": "0.8",
               "reason": "Back-dated correction received from quality control"}
    payload.update(changes)
    return payload


class StandardRevisionTests(Scenario):
    def amend(self, payload=None, expected=None):
        payload = payload or amendment()
        revision = self.app.status()["revision"] if expected is None else expected
        return self.app.amend_specification("G1", {**payload, "expected_revision": revision})

    def test_backdated_amendment_is_uniquely_effective(self):
        self.populated()
        result = self.amend()
        self.assertEqual(result["previous_spec_revision"], 1)
        self.assertEqual(result["proposed_spec_revision"], 2)
        self.assertEqual(self.app.quality_of_lot("LOT1")["spec_id"], "S2")
        timeline = self.app.specification_timeline("G1")
        self.assertEqual(timeline["current"]["spec_revision"], 2)
        self.assertEqual([segment["spec_id"] for segment in timeline["segments"]], ["S1", "S2"])
        self.assertEqual(timeline["segments"][0]["valid_to"], "2026-01-31")
        self.assertEqual(timeline["segments"][1]["valid_from"], "2026-02-01")

    def test_amendment_changes_old_lot_conclusion_and_explains_why(self):
        self.populated()
        preview = self.app.preview_amendment("G1", amendment())
        self.assertEqual(preview["impact"]["newly_failed_lots"], 1)
        lot = preview["impact"]["lots"][0]
        self.assertEqual(lot["lot_id"], "LOT1")
        self.assertEqual((lot["previous_status"], lot["current_status"]), ("passed", "failed"))
        self.assertEqual(lot["current_reasons"], ["sulfur_above_max"])
        self.assertIn("shipped_under_superseded_standard", lot["risk"])
        result = self.amend()
        self.assertEqual(result["impact"]["newly_failed_lots"], 1)
        self.assertEqual(self.app.quality_of_lot("LOT1")["status"], "failed")

    def test_unaffected_lots_are_not_reported_as_changed(self):
        self.populated()
        self.app.ingest({"source": "factory", "batch_id": "B2", "events": [
            {"kind": "lot", "external_id": "lot-2", "lot_id": "LOT2",
             "line_id": "L1", "grade": "G1",
             "produced_at": "2026-01-15T02:00:00Z", "quantity": "10"},
            {"kind": "assay", "external_id": "assay-2", "assay_id": "A2",
             "lot_id": "LOT2", "sampled_at": "2026-01-15T03:00:00Z",
             "sulfur": "0.7", "ash": "0.4"}]})
        result = self.amend()
        self.assertEqual(result["impact"]["examined_lots"], 2)
        self.assertEqual(result["impact"]["changed_lots"], 1)
        self.assertEqual(self.app.quality_of_lot("LOT2")["status"], "passed")
        self.assertEqual(self.app.quality_of_lot("LOT2")["spec_id"], "S1")

    def test_amendment_carve_keeps_earlier_and_later_edges(self):
        self.catalog()
        result = self.amend(amendment(valid_from="2026-02-10", valid_to="2026-02-20"))
        self.assertEqual([(segment["spec_id"], segment["valid_from"], segment["valid_to"])
                          for segment in result["resulting_timeline"]],
                         [("S1", "2026-01-01", "2026-02-09"),
                          ("S2", "2026-02-10", "2026-02-20"),
                          ("S1", "2026-02-21", None)])

    def test_amendment_can_be_closed_inside_its_period(self):
        self.catalog()
        result = self.amend(amendment(valid_from="2026-02-01", valid_to="2026-02-20"))
        self.assertEqual([(segment["spec_id"], segment["valid_from"], segment["valid_to"])
                          for segment in result["resulting_timeline"]],
                         [("S1", "2026-01-01", "2026-01-31"),
                          ("S2", "2026-02-01", "2026-02-20"),
                          ("S1", "2026-02-21", None)])

    def test_amendment_cannot_extend_past_containing_period(self):
        self.app.register_plant(dict(PLANT))
        self.app.register_line(dict(LINE))
        self.app.register_specification({**SPEC, "valid_to": "2026-02-28"})
        self.app.register_specification({**SPEC, "spec_id": "S9", "valid_from": "2026-03-01"})
        self.expect_domain(lambda: self.amend(amendment(valid_from="2026-02-10",
                                                        valid_to="2026-04-01")), "invalid_input")

    def test_amendment_before_catalogue_is_rejected(self):
        self.catalog()
        self.expect_domain(lambda: self.amend(amendment(valid_from="2025-12-01")),
                           "business_invariant")

    def test_amendment_requires_current_revision(self):
        self.catalog()
        self.expect_domain(lambda: self.amend(expected=0), "stale_revision")
        self.assertEqual(self.app.specification_timeline("G1")["current"]["spec_revision"], 1)

    def test_amendment_id_and_spec_id_are_idempotent_conflicts(self):
        self.catalog()
        self.amend()
        self.expect_domain(lambda: self.amend(amendment(spec_id="S3")), "conflict")
        self.expect_domain(lambda: self.amend(amendment(amendment_id="AM2")), "conflict")

    def test_old_revision_remains_readable(self):
        self.populated()
        self.amend()
        history = self.app.reassess_lot("LOT1")["revision_history"]
        self.assertEqual([(row["spec_revision"], row["classification"]["status"],
                           row["classification"]["spec_id"]) for row in history],
                         [(2, "failed", "S2"), (1, "passed", "S1")])

    def test_classification_names_the_timeline_revision_it_used(self):
        self.populated()
        before = self.app.quality_of_lot("LOT1")
        self.assertEqual(before["spec_revision"], 1)
        self.amend()
        after = self.app.quality_of_lot("LOT1")
        self.assertEqual(after["spec_revision"], 2)
        self.assertGreater(after["spec_accepted_revision"], before["spec_accepted_revision"])
        self.assertEqual(after["risk"]["current_spec_id"], "S2")

    def test_amendment_is_audited_with_impact(self):
        self.populated()
        revision = self.amend()["revision"]
        entry = self.app.trace.revision(revision)
        self.assertEqual(entry["audit"]["cause"], "specification_amendment")
        self.assertEqual(entry["entity"]["amendment_id"], "AM1")
        self.assertEqual(entry["entity"]["impact"]["newly_failed_lots"], 1)
        self.assertEqual(self.app.amendment("AM1")["grade"], "G1")

    def test_published_version_keeps_its_standard(self):
        self.populated()
        revision = self.app.status()["revision"]
        self.app.prepare({"candidate_id": "V1", "plant_id": "P1", "from_day": "2026-02-01",
                          "to_day": "2026-02-01", "expected_head": 0,
                          "expected_revision": revision})
        self.app.publish("V1", {"expected_head": 0, "expected_revision": revision})
        published = self.app.publication.version(1)
        self.assertEqual(published["specifications"]["G1"]["spec_revision"], 1)
        self.amend()
        frozen = self.app.publication.version(1)
        self.assertEqual(frozen["specifications"]["G1"]["spec_revision"], 1)
        self.assertEqual(frozen["rows"][0]["quality_counts"]["passed"], 1)
        comparison = self.app.compare("P1", "2026-02-01", "2026-02-01", 1)
        self.assertEqual(comparison["standard_changes"][0]["grade"], "G1")
        self.assertEqual(comparison["standard_changes"][0]["published_spec_revision"], 1)
        self.assertTrue(comparison["changes"][0]["quality_changed"])
        self.assertTrue(comparison["changes"][0]["standard_revision_changed"])

    def test_candidate_prepared_before_amendment_cannot_publish_twice_checked(self):
        self.catalog()
        self.ingest(LOT, ASSAY)
        revision = self.app.status()["revision"]
        self.app.prepare({"candidate_id": "V1", "plant_id": "P1", "from_day": "2026-02-01",
                          "to_day": "2026-02-01", "expected_head": 0,
                          "expected_revision": revision})
        self.amend()
        self.expect_domain(lambda: self.app.publish("V1", {
            "expected_head": 0, "expected_revision": self.app.status()["revision"]}),
            "stale_revision")
        self.assertEqual(self.app.status()["head"], 0)

    def test_live_quality_inventory_and_reservations_reflect_risk(self):
        self.populated()
        self.app.create_order(ORDER)
        self.app.reserve({"reservation_id": "R1", "order_id": "O1", "lot_id": "LOT1",
                          "quantity": "20", "expected_revision": self.app.status()["revision"]})
        result = self.amend()
        self.assertIn("reserved_under_superseded_standard",
                      result["impact"]["lots"][0]["risk"])
        quality = self.app.quality_of_lot("LOT1")
        self.assertEqual(quality["status"], "failed")
        self.assertTrue(quality["risk"]["on_quality_hold"])
        inventory = self.app.inventory("P1", "2026-02-02")
        self.assertEqual(inventory["remaining_tons"], "30")
        self.assertEqual(inventory["held_tons"], "30")
        self.assertEqual(inventory["available_tons"], "0")
        detail = self.app.orders.detail("O1")
        self.assertEqual(detail["at_risk_reservations"][0]["lot_id"], "LOT1")
        self.assertEqual(self.app.orders.allocation("P1", "2026-02-05")["at_risk_reservations"], 1)

    def test_failed_lot_cannot_be_reserved_after_amendment(self):
        self.populated()
        self.app.create_order(ORDER)
        self.amend()
        self.expect_domain(lambda: self.app.reserve({
            "reservation_id": "R2", "order_id": "O1", "lot_id": "LOT1", "quantity": "5",
            "expected_revision": self.app.status()["revision"]}), "business_invariant")

    def test_forecast_treats_held_reservations_as_unfilled(self):
        self.populated()
        self.app.create_order({**ORDER, "due_day": "2026-02-02", "quantity": "30"})
        self.app.reserve({"reservation_id": "R1", "order_id": "O1", "lot_id": "LOT1",
                          "quantity": "30", "expected_revision": self.app.status()["revision"]})
        before = self.app.forecast.horizon("P1", "2026-02-02", 1)
        self.assertEqual(before["rows"][0]["unfilled_due_tons"], "0")
        self.amend()
        after = self.app.forecast.horizon("P1", "2026-02-02", 1)
        self.assertEqual(after["rows"][0]["unfilled_due_tons"], "30")
        self.assertEqual(after["rows"][0]["at_risk_reserved_tons"], "30")

    def test_amendment_history_lists_revisions_and_impacts(self):
        self.populated()
        self.amend()
        history = self.app.amendment_history("G1")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["impact"]["newly_failed_lots"], 1)
        self.assertEqual(history[0]["impact"]["published_versions_affected"], [])

    def test_amendment_reports_published_versions_that_used_old_standard(self):
        self.populated()
        revision = self.app.status()["revision"]
        self.app.prepare({"candidate_id": "V1", "plant_id": "P1", "from_day": "2026-02-01",
                          "to_day": "2026-02-01", "expected_head": 0,
                          "expected_revision": revision})
        self.app.publish("V1", {"expected_head": 0, "expected_revision": revision})
        result = self.amend()
        affected = result["impact"]["published_versions_affected"]
        self.assertEqual([(row["version"], row["published_spec_revision"]) for row in affected],
                         [(1, 1)])
        self.assertEqual(self.app.publication.version(1)["specifications"]["G1"]["spec_revision"], 1)

    def test_api_amendment_workflow(self):
        self.populated()
        client = TestClient(create_app(self.path))
        status = client.get("/status").json()
        preview = client.post("/specifications/G1/amendments/preview", json=amendment())
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["impact"]["newly_failed_lots"], 1)
        applied = client.post("/specifications/G1/amendments",
                              json={**amendment(), "expected_revision": status["revision"]})
        self.assertEqual(applied.status_code, 201)
        timeline = client.get("/specifications/G1/timeline").json()
        self.assertEqual(timeline["current"]["spec_revision"], 2)
        history = client.get("/lots/LOT1/quality/history").json()
        self.assertEqual(history["current"]["status"], "failed")
        self.assertEqual(len(history["revision_history"]), 2)
        self.assertEqual(client.get("/amendments/AM1").json()["grade"], "G1")

    def test_legacy_database_is_migrated(self):
        path = self.path
        db = sqlite3.connect(path)
        db.executescript("""
            DROP TABLE specification_segments;
            DROP TABLE specification_revisions;
            DROP TABLE spec_amendments;
            DROP TABLE specifications;
            CREATE TABLE specifications (
                spec_id TEXT PRIMARY KEY, grade TEXT NOT NULL, valid_from TEXT NOT NULL,
                valid_to TEXT, sulfur_min TEXT NOT NULL, sulfur_max TEXT NOT NULL,
                ash_max TEXT NOT NULL, created_revision INTEGER NOT NULL,
                UNIQUE(grade,valid_from));
            INSERT INTO specifications VALUES ('S1','G1','2026-01-01',NULL,'0','1.2','0.8',3);
        """)
        db.commit()
        db.close()
        reopened = Service(path)
        self.assertEqual(reopened.specification_timeline("G1")["current"]["spec_revision"], 1)
        self.assertEqual(reopened.status()["head"], 0)
        revision = reopened.status()["revision"]
        reopened.amend_specification("G1", {**amendment(), "expected_revision": revision})
        self.assertEqual(reopened.specification_timeline("G1")["current"]["spec_revision"], 2)
