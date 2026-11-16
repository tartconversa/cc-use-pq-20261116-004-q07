import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .errors import Missing


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    revision INTEGER NOT NULL,
    head INTEGER NOT NULL
);
INSERT OR IGNORE INTO state VALUES (1,0,0);
CREATE TABLE IF NOT EXISTS plants (
    plant_id TEXT PRIMARY KEY, name TEXT NOT NULL, timezone TEXT NOT NULL,
    active_from TEXT NOT NULL, active_to TEXT, created_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS lines (
    line_id TEXT PRIMARY KEY, plant_id TEXT NOT NULL REFERENCES plants(plant_id),
    daily_capacity TEXT NOT NULL, active_from TEXT NOT NULL, active_to TEXT,
    created_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS specifications (
    spec_id TEXT PRIMARY KEY, grade TEXT NOT NULL, valid_from TEXT NOT NULL,
    valid_to TEXT, sulfur_min TEXT NOT NULL, sulfur_max TEXT NOT NULL,
    ash_max TEXT NOT NULL, created_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS specification_revisions (
    spec_revision INTEGER NOT NULL, grade TEXT NOT NULL,
    parent_revision INTEGER, reason TEXT NOT NULL, effective_from TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('current','superseded')),
    created_revision INTEGER NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(grade, spec_revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS specification_revision_current
    ON specification_revisions(grade) WHERE status='current';
CREATE TABLE IF NOT EXISTS specification_segments (
    grade TEXT NOT NULL, spec_revision INTEGER NOT NULL,
    spec_id TEXT NOT NULL REFERENCES specifications(spec_id),
    valid_from TEXT NOT NULL, valid_to TEXT,
    PRIMARY KEY(grade, spec_revision, valid_from),
    FOREIGN KEY(grade, spec_revision) REFERENCES specification_revisions(grade, spec_revision)
);
CREATE INDEX IF NOT EXISTS specification_segments_lookup
    ON specification_segments(grade, spec_revision, valid_from, valid_to);
CREATE TABLE IF NOT EXISTS lots (
    lot_id TEXT PRIMARY KEY, line_id TEXT NOT NULL REFERENCES lines(line_id),
    grade TEXT NOT NULL, produced_at TEXT NOT NULL, local_day TEXT NOT NULL,
    quantity TEXT NOT NULL, source TEXT NOT NULL, external_id TEXT NOT NULL,
    accepted_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS assays (
    assay_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES lots(lot_id),
    sampled_at TEXT NOT NULL, sulfur TEXT NOT NULL, ash TEXT NOT NULL,
    source TEXT NOT NULL, external_id TEXT NOT NULL, accepted_revision INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS assays_order ON assays(lot_id,sampled_at,accepted_revision);
CREATE TABLE IF NOT EXISTS shipments (
    shipment_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES lots(lot_id),
    shipped_at TEXT NOT NULL, local_day TEXT NOT NULL, quantity TEXT NOT NULL,
    destination TEXT NOT NULL, source TEXT NOT NULL, external_id TEXT NOT NULL,
    accepted_revision INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS shipments_lot ON shipments(lot_id);
CREATE TABLE IF NOT EXISTS receipts (
    source TEXT NOT NULL, external_id TEXT NOT NULL, payload_digest TEXT NOT NULL,
    accepted_revision INTEGER NOT NULL, event_kind TEXT NOT NULL,
    PRIMARY KEY(source, external_id)
);
CREATE TABLE IF NOT EXISTS rejections (
    batch_id TEXT NOT NULL, row_index INTEGER NOT NULL, source TEXT NOT NULL,
    external_id TEXT, error_code TEXT NOT NULL, reason TEXT NOT NULL,
    PRIMARY KEY(batch_id,row_index)
);
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY, source TEXT NOT NULL, request_digest TEXT NOT NULL,
    result_json TEXT NOT NULL, completed_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    revision INTEGER PRIMARY KEY, cause TEXT NOT NULL, ref TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY, plant_id TEXT NOT NULL REFERENCES plants(plant_id),
    from_day TEXT NOT NULL, to_day TEXT NOT NULL, base_revision INTEGER NOT NULL,
    expected_head INTEGER NOT NULL, rows_json TEXT NOT NULL, rows_digest TEXT NOT NULL,
    spec_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK(status IN ('prepared','published'))
);
CREATE TABLE IF NOT EXISTS publications (
    version INTEGER PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE REFERENCES candidates(candidate_id),
    plant_id TEXT NOT NULL, from_day TEXT NOT NULL, to_day TEXT NOT NULL,
    source_revision INTEGER NOT NULL, rows_digest TEXT NOT NULL,
    spec_json TEXT NOT NULL DEFAULT '{}',
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spec_amendments (
    amendment_id TEXT PRIMARY KEY, grade TEXT NOT NULL,
    from_spec_revision INTEGER, to_spec_revision INTEGER NOT NULL,
    spec_id TEXT NOT NULL REFERENCES specifications(spec_id),
    effective_from TEXT NOT NULL, reason TEXT NOT NULL,
    impact_json TEXT NOT NULL, applied_revision INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication_rows (
    version INTEGER NOT NULL REFERENCES publications(version), day TEXT NOT NULL,
    data_json TEXT NOT NULL, PRIMARY KEY(version,day)
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY, plant_id TEXT NOT NULL REFERENCES plants(plant_id),
    grade TEXT NOT NULL, due_day TEXT NOT NULL, quantity TEXT NOT NULL,
    destination TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','cancelled')),
    created_revision INTEGER NOT NULL, cancelled_revision INTEGER
);
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(order_id),
    lot_id TEXT NOT NULL REFERENCES lots(lot_id), quantity TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','released')),
    created_revision INTEGER NOT NULL, released_revision INTEGER,
    UNIQUE(order_id,lot_id,status)
);
CREATE INDEX IF NOT EXISTS reservations_lot ON reservations(lot_id,status);
CREATE TABLE IF NOT EXISTS source_policies (
    source TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    allowed_kinds TEXT NOT NULL, max_batch_size INTEGER NOT NULL,
    updated_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY, plant_id TEXT NOT NULL REFERENCES plants(plant_id),
    line_id TEXT NOT NULL REFERENCES lines(line_id),
    started_at TEXT NOT NULL, ended_at TEXT,
    start_day TEXT NOT NULL, end_day TEXT,
    severity TEXT NOT NULL CHECK(severity IN ('minor','major','critical')),
    reason TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','closed')),
    opened_revision INTEGER NOT NULL, closed_revision INTEGER
);
CREATE INDEX IF NOT EXISTS incidents_scope ON incidents(plant_id,start_day,end_day);
CREATE TABLE IF NOT EXISTS delivery_checks (
    check_id TEXT PRIMARY KEY, shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    checked_at TEXT NOT NULL, received_quantity TEXT NOT NULL,
    condition TEXT NOT NULL CHECK(condition IN ('accepted','damaged','rejected')),
    note TEXT NOT NULL, accepted_revision INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS delivery_checks_shipment ON delivery_checks(shipment_id,checked_at);
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(order_id),
    shipment_id TEXT NOT NULL UNIQUE REFERENCES shipments(shipment_id),
    quantity TEXT NOT NULL, accepted_revision INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=20000")
        return db

    def initialize(self):
        db = self.connect()
        try:
            db.executescript(SCHEMA)
            self._migrate(db)
            if self.path != ":memory:":
                db.execute("PRAGMA journal_mode=WAL")
        finally:
            db.close()

    @staticmethod
    def _columns(db, table: str) -> set[str]:
        return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}

    def _migrate(self, db):
        """Bring an existing database up to the current schema without touching data."""
        sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                         "AND name='specifications'").fetchone()
        legacy = bool(sql and "UNIQUE(grade,valid_from)" in sql[0].replace(" ", ""))
        if legacy:
            db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        try:
            for table in ("candidates", "publications"):
                if "spec_json" not in self._columns(db, table):
                    db.execute(f"ALTER TABLE {table} ADD COLUMN spec_json TEXT NOT NULL DEFAULT '{{}}'")
            # Older databases carried UNIQUE(grade,valid_from), which prevents a
            # revised standard from starting on a day an old row already starts.
            if legacy:
                # Rebuild in place: renaming would rewrite the segment table's
                # foreign key to the temporary name and leave it dangling.
                rows = [tuple(row) for row in db.execute(
                    "SELECT spec_id,grade,valid_from,valid_to,sulfur_min,sulfur_max,ash_max,"
                    "created_revision FROM specifications")]
                db.execute("DROP TABLE specifications")
                db.execute("CREATE TABLE specifications ("
                           "spec_id TEXT PRIMARY KEY, grade TEXT NOT NULL, valid_from TEXT NOT NULL,"
                           "valid_to TEXT, sulfur_min TEXT NOT NULL, sulfur_max TEXT NOT NULL,"
                           "ash_max TEXT NOT NULL, created_revision INTEGER NOT NULL)")
                db.executemany("INSERT INTO specifications VALUES (?,?,?,?,?,?,?,?)", rows)
            self._backfill_specification_revisions(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            if legacy:
                db.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _backfill_specification_revisions(db):
        """Seed revision 1 for catalogues created before revisions existed."""
        grades = [row[0] for row in db.execute(
            "SELECT DISTINCT grade FROM specifications WHERE grade NOT IN "
            "(SELECT grade FROM specification_revisions) ORDER BY grade")]
        for grade in grades:
            db.execute("INSERT INTO specification_revisions VALUES (?,?,?,?,?,?,?,?)",
                       (1, grade, None, "initial catalogue",
                        db.execute("SELECT min(valid_from) FROM specifications WHERE grade=?",
                                   (grade,)).fetchone()[0], "current",
                        db.execute("SELECT min(created_revision) FROM specifications WHERE grade=?",
                                   (grade,)).fetchone()[0],
                        db.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]))
            for row in db.execute("SELECT * FROM specifications WHERE grade=? ORDER BY valid_from",
                                  (grade,)):
                db.execute("INSERT INTO specification_segments VALUES (?,?,?,?,?)",
                           (grade, 1, row["spec_id"], row["valid_from"], row["valid_to"]))

    @contextmanager
    def read(self):
        db = self.connect()
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def write(self):
        db = self.connect()
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def revision(db) -> int:
        return db.execute("SELECT revision FROM state WHERE singleton=1").fetchone()[0]

    @staticmethod
    def head(db) -> int:
        return db.execute("SELECT head FROM state WHERE singleton=1").fetchone()[0]

    @staticmethod
    def advance(db, cause: str, ref: str) -> int:
        db.execute("UPDATE state SET revision=revision+1 WHERE singleton=1")
        revision = Store.revision(db)
        db.execute("INSERT INTO audit VALUES (?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                   (revision, cause, ref))
        return revision

    @staticmethod
    def one(db, sql: str, params: tuple, entity: str):
        row = db.execute(sql, params).fetchone()
        if row is None:
            raise Missing(f"{entity} not found")
        return row

    def status(self) -> dict:
        with self.read() as db:
            return {"revision": self.revision(db), "head": self.head(db),
                    "plants": db.execute("SELECT count(*) FROM plants").fetchone()[0],
                    "lots": db.execute("SELECT count(*) FROM lots").fetchone()[0]}
