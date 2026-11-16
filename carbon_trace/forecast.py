from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from .catalog import Catalog
from .errors import require
from .orders import Orders
from .quality import Quality
from .store import Store
from .values import as_number, day, decimal, identifier


class Forecast:
    def __init__(self, store: Store):
        self.store = store

    def horizon(self, plant_id: str, start_day: str, days: int) -> dict:
        plant_id = identifier(plant_id, "plant_id")
        start_day = day(start_day, "start_day")
        require(type(days) is int and 1 <= days <= 90, "days must be 1-90")
        quality = Quality()
        with self.store.read() as db:
            plant = Catalog.plant(db, plant_id)
            final = (date.fromisoformat(start_day) + timedelta(days=days - 1)).isoformat()
            lines = db.execute("SELECT * FROM lines WHERE plant_id=? ORDER BY line_id", (plant_id,)).fetchall()
            orders = db.execute("SELECT * FROM orders WHERE plant_id=? AND status='open' "
                                "AND due_day BETWEEN ? AND ? ORDER BY due_day,order_id",
                                (plant_id, start_day, final)).fetchall()
            due = defaultdict(lambda: Decimal(0))
            at_risk = defaultdict(lambda: Decimal(0))
            for order in orders:
                active = db.execute("SELECT reservations.quantity,lots.* FROM reservations JOIN lots "
                                    "ON lots.lot_id=reservations.lot_id WHERE reservations.order_id=? "
                                    "AND reservations.status='active'", (order["order_id"],)).fetchall()
                risky = sum((decimal(row["quantity"], "quantity") for row in active
                             if quality.risk(db, row)["on_quality_hold"]), Decimal(0))
                # Reservations on material that now fails are not dependable supply.
                outstanding = (decimal(order["quantity"], "quantity") - Orders._sum(
                    db, "SELECT quantity FROM reservations WHERE order_id=? AND status='active'",
                    order["order_id"]) + risky)
                due[order["due_day"]] += outstanding
                at_risk[order["due_day"]] += risky
            production = db.execute("SELECT local_day,quantity FROM lots JOIN lines "
                                    "ON lots.line_id=lines.line_id WHERE lines.plant_id=? "
                                    "AND local_day BETWEEN ? AND ?", (plant_id, start_day, final)).fetchall()
            recorded = defaultdict(lambda: Decimal(0))
            for row in production:
                recorded[row["local_day"]] += decimal(row["quantity"], "quantity")
            projected = []
            backlog = Decimal(0)
            for offset in range(days):
                target = (date.fromisoformat(start_day) + timedelta(days=offset)).isoformat()
                active_lines = [line for line in lines if Catalog.valid_on(plant["active_from"], plant["active_to"], target)
                          and Catalog.valid_on(line["active_from"], line["active_to"], target)]
                capacity = sum((decimal(line["daily_capacity"], "capacity") for line in active_lines), Decimal(0))
                current_due = due[target]
                available = max(Decimal(0), capacity - recorded[target])
                demand = backlog + current_due
                served = min(available, demand)
                backlog = demand - served
                projected.append({"day": target, "active_lines": len(active_lines),
                                  "capacity_tons": as_number(capacity),
                                  "recorded_production_tons": as_number(recorded[target]),
                                  "available_tons": as_number(available),
                                  "unfilled_due_tons": as_number(current_due),
                                  "at_risk_reserved_tons": as_number(at_risk[target]),
                                  "modeled_service_tons": as_number(served),
                                  "end_backlog_tons": as_number(backlog)})
            return {"plant_id": plant_id, "start_day": start_day, "days": days,
                    "source_revision": Store.revision(db), "rows": projected,
                    "ending_backlog_tons": as_number(backlog),
                    "total_at_risk_tons": as_number(sum(at_risk.values(), Decimal(0))),
                    "total_due_tons": as_number(sum(due.values(), Decimal(0)))}

