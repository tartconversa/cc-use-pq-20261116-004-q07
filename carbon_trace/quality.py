from decimal import Decimal

from .catalog import Catalog
from .errors import Invariant, Missing
from .store import Store
from .values import as_number, decimal, identifier


class Quality:
    def latest_assay(self, db, lot_id: str, cutoff: str | None = None):
        query = "SELECT * FROM assays WHERE lot_id=?"
        params: tuple = (lot_id,)
        if cutoff is not None:
            query += " AND sampled_at<=?"
            params += (cutoff,)
        query += " ORDER BY sampled_at DESC,accepted_revision DESC,assay_id DESC LIMIT 1"
        return db.execute(query, params).fetchone()

    def classify(self, db, lot, *, cutoff: str | None = None, spec_revision: int | None = None,
                 spec_timeline: list[dict] | None = None) -> dict:
        spec = self._specification(db, lot, spec_revision, spec_timeline)
        assay = self.latest_assay(db, lot["lot_id"], cutoff)
        base = {"lot_id": lot["lot_id"], "grade": lot["grade"],
                "spec_id": spec["spec_id"], "spec_revision": spec["timeline_revision"],
                "spec_accepted_revision": spec["created_revision"],
                "assay_id": None, "assay_revision": None, "status": "unmeasured",
                "sulfur": None, "ash": None}
        if assay is None:
            return base
        sulfur = decimal(assay["sulfur"], "sulfur")
        ash = decimal(assay["ash"], "ash")
        low = decimal(spec["sulfur_min"], "sulfur_min")
        high = decimal(spec["sulfur_max"], "sulfur_max")
        ash_max = decimal(spec["ash_max"], "ash_max")
        reasons = []
        if sulfur < low:
            reasons.append("sulfur_below_min")
        if sulfur > high:
            reasons.append("sulfur_above_max")
        if ash > ash_max:
            reasons.append("ash_above_max")
        return {**base, "assay_id": assay["assay_id"],
                "assay_revision": assay["accepted_revision"],
                "status": "failed" if reasons else "passed", "reasons": reasons,
                "sulfur": assay["sulfur"], "ash": assay["ash"]}

    @staticmethod
    def _specification(db, lot, spec_revision: int | None,
                       spec_timeline: list[dict] | None) -> dict:
        if spec_timeline is None and spec_revision is None:
            return Catalog.specification(db, lot["grade"], lot["local_day"])
        if spec_timeline is None:
            spec_timeline = Catalog.segments(db, lot["grade"], spec_revision)
        matches = [segment for segment in spec_timeline
                   if segment["valid_from"] <= lot["local_day"] and
                   (segment["valid_to"] is None or lot["local_day"] <= segment["valid_to"])]
        if not matches:
            raise Missing("no specification for grade and day in the requested revision")
        if len(matches) != 1:
            raise Invariant("ambiguous grade specification in the requested revision")
        segment = matches[0]
        return {"spec_id": segment["spec_id"], "sulfur_min": segment["sulfur_min"],
                "sulfur_max": segment["sulfur_max"], "ash_max": segment["ash_max"],
                "created_revision": segment.get("created_revision"),
                "timeline_revision": spec_revision}

    @staticmethod
    def shipped_quantity(db, lot_id: str) -> Decimal:
        return sum((decimal(row["quantity"], "quantity") for row in db.execute(
            "SELECT quantity FROM shipments WHERE lot_id=?", (lot_id,))), Decimal(0))

    @staticmethod
    def reserved_quantity(db, lot_id: str) -> Decimal:
        return sum((decimal(row["quantity"], "quantity") for row in db.execute(
            "SELECT quantity FROM reservations WHERE lot_id=? AND status='active'", (lot_id,))),
            Decimal(0))

    @staticmethod
    def committed(db, lot_id: str) -> bool:
        return db.execute("SELECT 1 FROM commitments JOIN shipments ON "
                          "shipments.shipment_id=commitments.shipment_id WHERE shipments.lot_id=?",
                          (lot_id,)).fetchone() is not None

    def risk(self, db, lot, classification: dict | None = None) -> dict:
        """Exposure of a lot under the *currently effective* standard."""
        result = classification or self.classify(db, lot)
        shipped = self.shipped_quantity(db, lot["lot_id"])
        reserved = self.reserved_quantity(db, lot["lot_id"])
        committed = self.committed(db, lot["lot_id"])
        flags = []
        if result["status"] == "failed":
            if shipped:
                flags.append("shipped_under_failed_standard")
            if reserved:
                flags.append("reserved_under_failed_standard")
            if committed:
                flags.append("committed_under_failed_standard")
            flags.append("material_hold")
        elif result["status"] == "unmeasured":
            if shipped:
                flags.append("shipped_unmeasured")
        return {"quality_status": result["status"], "current_spec_id": result["spec_id"],
                "spec_revision": result["spec_revision"],
                "shipped_tons": as_number(shipped), "reserved_tons": as_number(reserved),
                "on_quality_hold": bool(flags and "material_hold" in flags),
                "flags": flags}

    def distribution(self, db, lots: list) -> dict:
        counts = {"passed": 0, "failed": 0, "unmeasured": 0}
        quantities = {key: Decimal(0) for key in counts}
        classifications = {}
        for lot in lots:
            result = self.classify(db, lot)
            classifications[lot["lot_id"]] = result
            counts[result["status"]] += 1
            quantities[result["status"]] += decimal(lot["quantity"], "quantity")
        return {"counts": counts, "quantities": {key: str(value) for key, value in quantities.items()},
                "lots": classifications}

    def lot(self, store: Store, lot_id: str) -> dict:
        with store.read() as db:
            lot = Store.one(db, "SELECT * FROM lots WHERE lot_id=?", (lot_id,), "lot")
            result = self.classify(db, lot)
            return {**result, "risk": self.risk(db, lot, result),
                    "source_revision": Store.revision(db)}

    def reassess(self, store: Store, lot_id: str) -> dict:
        """Classify a lot under every retained revision, newest first."""
        lot_id = identifier(lot_id, "lot_id")
        with store.read() as db:
            lot = Store.one(db, "SELECT * FROM lots WHERE lot_id=?", (lot_id,), "lot")
            current = self.classify(db, lot)
            revisions = [dict(row) for row in db.execute(
                "SELECT * FROM specification_revisions WHERE grade=? ORDER BY spec_revision DESC",
                (lot["grade"],))]
            history = []
            for revision in revisions:
                assessment = self.classify(db, lot, spec_revision=revision["spec_revision"])
                history.append({"spec_revision": revision["spec_revision"],
                                "timeline_status": revision["status"],
                                "reason": revision["reason"],
                                "effective_from": revision["effective_from"],
                                "applied_revision": revision["created_revision"],
                                "classification": assessment})
            return {"lot_id": lot_id, "grade": lot["grade"], "local_day": lot["local_day"],
                    "current": current, "risk": self.risk(db, lot, current),
                    "revision_history": history,
                    "source_revision": Store.revision(db)}

