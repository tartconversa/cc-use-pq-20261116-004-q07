"""Auditable revision of grade standards.

Every change to a grade catalogue creates an immutable revision with a complete,
non-overlapping effective timeline.  A back-dated correction is applied as an
amendment: the covered date range is carved out of the previous timeline, the
new standard is inserted, and the impact on already-recorded lots is computed
and stored with the amendment.  Old revisions, old publications and the lots
themselves are never rewritten.
"""
import json
from datetime import date, timedelta

from .errors import Conflict, Invariant, Stale, require
from .quality import Quality
from .store import Store
from .values import as_number, day, decimal, identifier, text


def _previous(value: str) -> str:
    return (date.fromisoformat(value) - timedelta(days=1)).isoformat()


def _following(value: str) -> str:
    return (date.fromisoformat(value) + timedelta(days=1)).isoformat()


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _comparable(segments: list[dict]) -> list[dict]:
    return [{key: segment.get(key) for key in
             ("spec_id", "valid_from", "valid_to", "sulfur_min", "sulfur_max", "ash_max")}
            for segment in segments]


def decode(row) -> dict:
    """Expose a stored amendment with its impact and timeline expanded."""
    item = dict(row)
    stored = json.loads(item.pop("impact_json"))
    item["impact"] = stored.pop("impact")
    item.update(stored)
    return item


class Standards:
    def __init__(self, store: Store):
        self.store = store
        self.quality = Quality()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _segments(db, grade: str, spec_revision: int) -> list[dict]:
        from .catalog import Catalog
        return Catalog.segments(db, grade, spec_revision)

    @staticmethod
    def _validate(first, last, low, high) -> None:
        if last is not None:
            require(last >= first, "valid_to precedes valid_from")
        require(low <= high, "sulfur interval is inverted")

    def _carve(self, segments: list[dict], first: str, last: str | None) -> list[dict]:
        """Remove ``[first, last]`` from a timeline, splitting retained edges."""
        result = []
        for segment in segments:
            start, end = segment["valid_from"], segment["valid_to"]
            overlaps = (end is None or first <= end) and (last is None or start <= last)
            if not overlaps:
                result.append(segment)
                continue
            if start < first:
                result.append({**segment, "valid_to": _previous(first)})
            if last is not None and (end is None or last < end):
                result.append({**segment, "valid_from": _following(last)})
        return result

    def _next_revision(self, db, grade: str, current: int) -> int:
        row = db.execute("SELECT max(spec_revision) FROM specification_revisions WHERE grade=?",
                         (grade,)).fetchone()
        return max(row[0] or 0, current) + 1

    # ---------------------------------------------------------------- amendment
    def history(self, grade: str) -> list[dict]:
        grade = identifier(grade, "grade")
        with self.store.read() as db:
            return [decode(row) for row in db.execute(
                "SELECT * FROM spec_amendments WHERE grade=? ORDER BY applied_revision", (grade,))]

    def amendment(self, amendment_id: str) -> dict:
        amendment_id = identifier(amendment_id, "amendment_id")
        with self.store.read() as db:
            return decode(Store.one(db, "SELECT * FROM spec_amendments WHERE amendment_id=?",
                                     (amendment_id,), "amendment"))

    @classmethod
    def timeline(cls, db, grade: str) -> dict:
        from .catalog import Catalog
        result = Catalog.timeline(db, grade)
        for revision in result["revisions"]:
            revision["segments"] = cls._segments(db, grade, revision["spec_revision"])
        result["amendments"] = [decode(row) for row in db.execute(
            "SELECT * FROM spec_amendments WHERE grade=? ORDER BY applied_revision", (grade,))]
        return result

    def preview(self, grade: str, payload: dict) -> dict:
        grade = identifier(grade, "grade")
        details = self._amendment_input(grade, payload)
        with self.store.read() as db:
            current = self._current(db, grade)
            resulting = self._propose(db, grade, current, details)
            return self._impact(db, grade, current["spec_revision"], resulting,
                                current["spec_revision"] + 1, details["valid_from"])

    def _propose(self, db, grade: str, current, details: dict) -> list[dict]:
        base = self._segments(db, grade, current["spec_revision"])
        containing = [segment for segment in base
                      if segment["valid_from"] <= details["valid_from"] and
                      (segment["valid_to"] is None or details["valid_from"] <= segment["valid_to"])]
        if not containing:
            raise Invariant("amendment effective date does not fall in an existing standard period")
        if len(containing) != 1:
            raise Invariant("current standard timeline is ambiguous at effective date")
        limit = containing[0]["valid_to"]
        last = details["valid_to"]
        if last is None:
            # An open-ended correction replaces the rest of the period it corrects.
            last = limit
        elif limit is not None:
            require(last <= limit, "amendment must end within the period being corrected; "
                                   "amend the later standard separately")
        details["valid_to"] = last
        proposed = {"spec_id": details["spec_id"], "valid_from": details["valid_from"],
                    "valid_to": last,
                    "sulfur_min": as_number(details["sulfur_min"]),
                    "sulfur_max": as_number(details["sulfur_max"]),
                    "ash_max": as_number(details["ash_max"])}
        return sorted(self._carve(base, details["valid_from"], last) + [proposed],
                      key=lambda segment: segment["valid_from"])

    def amend(self, grade: str, payload: dict) -> dict:
        grade = identifier(grade, "grade")
        details = self._amendment_input(grade, payload)
        expected = payload.get("expected_revision")
        require(type(expected) is int and expected >= 0, "invalid expected_revision")
        with self.store.write() as db:
            if Store.revision(db) != expected:
                raise Stale("source revision changed before amendment")
            current = self._current(db, grade)
            resulting = self._propose(db, grade, current, details)
            if db.execute("SELECT 1 FROM spec_amendments WHERE amendment_id=?",
                          (details["amendment_id"],)).fetchone():
                raise Conflict("amendment_id already exists")
            if db.execute("SELECT 1 FROM specifications WHERE spec_id=?",
                          (details["spec_id"],)).fetchone():
                raise Conflict("spec_id already exists")
            new_revision = self._next_revision(db, grade, current["spec_revision"])
            impact = self._impact(db, grade, current["spec_revision"], resulting,
                                  new_revision, details["valid_from"])
            revision = Store.advance(db, "specification_amendment", details["amendment_id"])
            db.execute("INSERT INTO specifications VALUES (?,?,?,?,?,?,?,?)",
                       (details["spec_id"], grade, details["valid_from"], details["valid_to"],
                        as_number(details["sulfur_min"]), as_number(details["sulfur_max"]),
                        as_number(details["ash_max"]), revision))
            db.execute("UPDATE specification_revisions SET status='superseded' "
                       "WHERE grade=? AND status='current'", (grade,))
            db.execute("INSERT INTO specification_revisions VALUES (?,?,?,?,?,?,?,?)",
                       (new_revision, grade, current["spec_revision"], details["reason"],
                        details["valid_from"], "current", revision, _now()))
            db.executemany("INSERT INTO specification_segments VALUES (?,?,?,?,?)",
                           [(grade, new_revision, segment["spec_id"], segment["valid_from"],
                             segment["valid_to"]) for segment in impact["resulting_timeline"]])
            db.execute("INSERT INTO spec_amendments VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (details["amendment_id"], grade, current["spec_revision"], new_revision,
                        details["spec_id"], details["valid_from"], details["reason"],
                        _json(impact), revision, _now()))
            return {"amendment_id": details["amendment_id"], "grade": grade,
                    "revision": revision, "previous_spec_revision": current["spec_revision"],
                    **impact, "applied": True}

    def _amendment_input(self, grade: str, payload: dict) -> dict:
        amendment_id = identifier(payload.get("amendment_id"), "amendment_id")
        spec_id = identifier(payload.get("spec_id"), "spec_id")
        first = day(payload.get("valid_from"), "valid_from")
        last = payload.get("valid_to")
        if last is not None:
            last = day(last, "valid_to")
        low = decimal(payload.get("sulfur_min"), "sulfur_min", nonnegative=True)
        high = decimal(payload.get("sulfur_max"), "sulfur_max", nonnegative=True)
        ash = decimal(payload.get("ash_max"), "ash_max", nonnegative=True)
        self._validate(first, last, low, high)
        reason = text(payload.get("reason"), "reason", max_length=400)
        return {"amendment_id": amendment_id, "spec_id": spec_id, "valid_from": first,
                "valid_to": last, "sulfur_min": low, "sulfur_max": high, "ash_max": ash,
                "reason": reason}

    @staticmethod
    def _current(db, grade: str):
        return Store.one(db, "SELECT * FROM specification_revisions WHERE grade=? "
                             "AND status='current'", (grade,), "current specification revision")

    def _impact(self, db, grade: str, old_revision: int, resulting: list[dict],
                new_revision: int, effective_from: str) -> dict:
        base = self._segments(db, grade, old_revision)
        lots = db.execute("SELECT * FROM lots WHERE grade=? ORDER BY local_day,lot_id",
                          (grade,)).fetchall()
        changed, newly_failed, newly_passed = [], 0, 0
        for lot in lots:
            old = self.quality.classify(db, lot, spec_revision=old_revision)
            new = self.quality.classify(db, lot, spec_revision=new_revision,
                                        spec_timeline=resulting)
            if old["status"] == new["status"] and old["spec_id"] == new["spec_id"]:
                continue
            shipped = self.quality.shipped_quantity(db, lot["lot_id"])
            reserved = self.quality.reserved_quantity(db, lot["lot_id"])
            flags = []
            if new["status"] == "failed":
                if shipped:
                    flags.append("shipped_under_superseded_standard")
                if reserved:
                    flags.append("reserved_under_superseded_standard")
                if self.quality.committed(db, lot["lot_id"]):
                    flags.append("committed_delivery_under_superseded_standard")
            newly_failed += new["status"] == "failed" and old["status"] != "failed"
            newly_passed += new["status"] == "passed" and old["status"] != "passed"
            changed.append({"lot_id": lot["lot_id"], "local_day": lot["local_day"],
                            "previous_status": old["status"], "current_status": new["status"],
                            "previous_spec_id": old["spec_id"], "current_spec_id": new["spec_id"],
                            "previous_reasons": old.get("reasons", []),
                            "current_reasons": new.get("reasons", []),
                            "assay_id": new["assay_id"],
                            "shipped_tons": as_number(shipped),
                            "reserved_tons": as_number(reserved),
                            "risk": flags})
        return {"previous_spec_revision": old_revision,
                "proposed_spec_revision": new_revision,
                "effective_from": effective_from,
                "resulting_timeline": resulting,
                "previously_effective_timeline": base,
                "impact": {"examined_lots": len(lots), "changed_lots": len(changed),
                           "newly_failed_lots": newly_failed, "newly_passed_lots": newly_passed,
                           "shipped_lots_at_risk": sum(
                               "shipped_under_superseded_standard" in item["risk"]
                               for item in changed),
                           "reserved_lots_at_risk": sum(
                               "reserved_under_superseded_standard" in item["risk"]
                               for item in changed),
                           "published_versions_affected": self._published_versions(
                               db, grade, resulting),
                           "lots": changed}}

    @staticmethod
    def _published_versions(db, grade: str, resulting: list[dict]) -> list[dict]:
        """Publications whose frozen standard snapshot would differ from the new one."""
        affected = []
        for row in db.execute("SELECT version,plant_id,from_day,to_day,spec_json,source_revision "
                              "FROM publications ORDER BY version"):
            snapshot = json.loads(row["spec_json"] or "{}")
            prior = snapshot.get(grade)
            if prior is None:
                continue
            if _comparable(prior.get("segments", [])) != _comparable(resulting):
                affected.append({"version": row["version"], "plant_id": row["plant_id"],
                                 "from_day": row["from_day"], "to_day": row["to_day"],
                                 "published_spec_revision": prior.get("spec_revision"),
                                 "published_revision": row["source_revision"]})
        return affected


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
