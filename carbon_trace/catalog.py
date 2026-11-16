from .errors import Conflict, Invariant, Missing, require
from .store import Store
from .values import as_number, day, decimal, identifier, text, zone_name


class Catalog:
    def __init__(self, store: Store):
        self.store = store

    def add_plant(self, payload: dict) -> dict:
        plant_id = identifier(payload.get("plant_id"), "plant_id")
        name = text(payload.get("name"), "name")
        zone = zone_name(payload.get("timezone"))
        first = day(payload.get("active_from"), "active_from")
        last = payload.get("active_to")
        if last is not None:
            last = day(last, "active_to")
            require(last >= first, "active_to precedes active_from")
        with self.store.write() as db:
            if db.execute("SELECT 1 FROM plants WHERE plant_id=?", (plant_id,)).fetchone():
                raise Conflict("plant_id already exists")
            revision = self.store.advance(db, "plant", plant_id)
            db.execute("INSERT INTO plants VALUES (?,?,?,?,?,?)",
                       (plant_id, name, zone, first, last, revision))
            return {"plant_id": plant_id, "revision": revision}

    def add_line(self, payload: dict) -> dict:
        line_id = identifier(payload.get("line_id"), "line_id")
        plant_id = identifier(payload.get("plant_id"), "plant_id")
        capacity = as_number(decimal(payload.get("daily_capacity"), "daily_capacity", positive=True))
        first = day(payload.get("active_from"), "active_from")
        last = payload.get("active_to")
        if last is not None:
            last = day(last, "active_to")
            require(last >= first, "active_to precedes active_from")
        with self.store.write() as db:
            plant = self.plant(db, plant_id)
            require(first >= plant["active_from"] and
                    (plant["active_to"] is None or first <= plant["active_to"]),
                    "line start lies outside plant lifecycle")
            if last is not None:
                require(plant["active_to"] is None or last <= plant["active_to"],
                        "line end lies outside plant lifecycle")
            if db.execute("SELECT 1 FROM lines WHERE line_id=?", (line_id,)).fetchone():
                raise Conflict("line_id already exists")
            revision = self.store.advance(db, "line", line_id)
            db.execute("INSERT INTO lines VALUES (?,?,?,?,?,?)",
                       (line_id, plant_id, capacity, first, last, revision))
            return {"line_id": line_id, "revision": revision}

    def add_specification(self, payload: dict) -> dict:
        spec_id = identifier(payload.get("spec_id"), "spec_id")
        grade = identifier(payload.get("grade"), "grade")
        first = day(payload.get("valid_from"), "valid_from")
        last = payload.get("valid_to")
        if last is not None:
            last = day(last, "valid_to")
            require(last >= first, "valid_to precedes valid_from")
        low = decimal(payload.get("sulfur_min"), "sulfur_min", nonnegative=True)
        high = decimal(payload.get("sulfur_max"), "sulfur_max", nonnegative=True)
        ash = decimal(payload.get("ash_max"), "ash_max", nonnegative=True)
        require(low <= high, "sulfur interval is inverted")
        with self.store.write() as db:
            if db.execute("SELECT 1 FROM specifications WHERE spec_id=?", (spec_id,)).fetchone():
                raise Conflict("spec_id already exists")
            current = db.execute("SELECT * FROM specification_revisions WHERE grade=? "
                                 "AND status='current'", (grade,)).fetchone()
            old_revision = current["spec_revision"] if current else 0
            segments = (self.segments(db, grade, old_revision) if current else [])
            for old in segments:
                if (old["valid_to"] is None or first <= old["valid_to"]) and (last is None or old["valid_from"] <= last):
                    raise Conflict("grade specification periods overlap")
            revision = self.store.advance(db, "specification", spec_id)
            db.execute("INSERT INTO specifications VALUES (?,?,?,?,?,?,?,?)",
                       (spec_id, grade, first, last, as_number(low), as_number(high), as_number(ash), revision))
            spec_revision = old_revision + 1 if current else 1
            if current:
                db.execute("UPDATE specification_revisions SET status='superseded' "
                           "WHERE grade=? AND status='current'", (grade,))
            db.execute("INSERT INTO specification_revisions VALUES (?,?,?,?,?,?,?,?)",
                       (spec_revision, grade, old_revision or None,
                        "catalogue addition" if current else "initial catalogue",
                        first if not current else
                        db.execute("SELECT min(valid_from) FROM specification_segments WHERE grade=? "
                                   "AND spec_revision=?", (grade, old_revision)).fetchone()[0],
                        "current", revision,
                        db.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]))
            db.executemany("INSERT INTO specification_segments VALUES (?,?,?,?,?)",
                           [(grade, spec_revision, segment["spec_id"], segment["valid_from"],
                             segment["valid_to"]) for segment in segments] +
                           [(grade, spec_revision, spec_id, first, last)])
            return {"spec_id": spec_id, "grade": grade, "revision": revision,
                    "spec_revision": spec_revision}

    @staticmethod
    def plant(db, plant_id: str):
        return Store.one(db, "SELECT * FROM plants WHERE plant_id=?", (plant_id,), "plant")

    @staticmethod
    def line(db, line_id: str):
        return Store.one(db, "SELECT l.*,p.timezone,p.active_from AS plant_from,p.active_to AS plant_to "
                         "FROM lines l JOIN plants p ON p.plant_id=l.plant_id WHERE l.line_id=?",
                         (line_id,), "line")

    @staticmethod
    def valid_on(first: str, last: str | None, target: str) -> bool:
        return first <= target and (last is None or target <= last)

    @classmethod
    def active_line(cls, db, line_id: str, local_day: str):
        line = cls.line(db, line_id)
        if not cls.valid_on(line["active_from"], line["active_to"], local_day):
            raise Invariant("line inactive on event day")
        if not cls.valid_on(line["plant_from"], line["plant_to"], local_day):
            raise Invariant("plant inactive on event day")
        return line

    @staticmethod
    def specification(db, grade: str, local_day: str):
        rows = db.execute("SELECT s.spec_id,g.valid_from,g.valid_to,s.sulfur_min,s.sulfur_max,"
                          "s.ash_max,s.created_revision,r.spec_revision AS timeline_revision "
                          "FROM specifications s JOIN specification_segments g "
                          "ON g.spec_id=s.spec_id AND g.grade=s.grade "
                          "JOIN specification_revisions r ON r.grade=g.grade "
                          "AND r.spec_revision=g.spec_revision AND r.status='current' "
                          "WHERE s.grade=? AND g.valid_from<=? "
                          "AND (g.valid_to IS NULL OR g.valid_to>=?) ORDER BY g.valid_from DESC",
                          (grade, local_day, local_day)).fetchall()
        if not rows:
            raise Missing("no specification for grade and day")
        if len(rows) != 1:
            raise Invariant("ambiguous grade specification")
        return rows[0]

    @staticmethod
    def segments(db, grade: str, spec_revision: int) -> list[dict]:
        return [dict(row) for row in db.execute(
            "SELECT s.spec_id, g.valid_from, g.valid_to, s.sulfur_min, s.sulfur_max, s.ash_max,"
            "s.created_revision FROM specification_segments g JOIN specifications s "
            "ON s.spec_id=g.spec_id WHERE g.grade=? AND g.spec_revision=? ORDER BY g.valid_from",
            (grade, spec_revision))]

    @staticmethod
    def timeline(db, grade: str) -> dict:
        current = db.execute("SELECT * FROM specification_revisions WHERE grade=? "
                             "AND status='current'", (grade,)).fetchone()
        if current is None:
            raise Missing("no specification revision for grade")
        revisions = [dict(row) for row in db.execute(
            "SELECT * FROM specification_revisions WHERE grade=? ORDER BY spec_revision DESC",
            (grade,))]
        return {"grade": grade, "current": dict(current),
                "segments": Catalog.segments(db, grade, current["spec_revision"]),
                "revisions": revisions}

    def list_plants(self) -> list[dict]:
        with self.store.read() as db:
            return [dict(row) for row in db.execute("SELECT * FROM plants ORDER BY plant_id")]

    def list_lines(self, plant_id: str) -> list[dict]:
        with self.store.read() as db:
            self.plant(db, identifier(plant_id, "plant_id"))
            return [dict(row) for row in db.execute("SELECT * FROM lines WHERE plant_id=? ORDER BY line_id", (plant_id,))]

    @staticmethod
    def timeline_snapshot(db, grades: list[str]) -> dict:
        result = {}
        for grade in sorted(set(grades)):
            row = db.execute("SELECT * FROM specification_revisions WHERE grade=? "
                             "AND status='current'", (grade,)).fetchone()
            if row is None:
                continue
            result[grade] = {"spec_revision": row["spec_revision"],
                             "segments": Catalog.segments(db, grade, row["spec_revision"])}
        return result

    def list_specifications(self, grade: str) -> list[dict]:
        with self.store.read() as db:
            return [dict(row) for row in db.execute("SELECT * FROM specifications WHERE grade=? ORDER BY valid_from",
                                                    (identifier(grade, "grade"),))]
