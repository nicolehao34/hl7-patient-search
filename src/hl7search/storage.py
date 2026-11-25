from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import ParsedMessage


class DuplicateMessageError(ValueError):
    pass


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS patients (
                    id INTEGER PRIMARY KEY, external_id TEXT NOT NULL,
                    assigning_authority TEXT NOT NULL DEFAULT '', family_name TEXT,
                    given_name TEXT, birth_date TEXT, sex TEXT, updated_at TEXT NOT NULL,
                    UNIQUE(external_id, assigning_authority));
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, sending_application TEXT NOT NULL,
                    sending_facility TEXT NOT NULL, control_id TEXT NOT NULL,
                    message_type TEXT NOT NULL, trigger_event TEXT NOT NULL,
                    version TEXT, message_at TEXT NOT NULL, received_at TEXT NOT NULL,
                    patient_id INTEGER REFERENCES patients(id), raw TEXT NOT NULL,
                    UNIQUE(sending_application, sending_facility, control_id));
                CREATE TABLE IF NOT EXISTS encounters (
                    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id),
                    visit_number TEXT NOT NULL, facility TEXT NOT NULL, patient_class TEXT,
                    location TEXT, admit_at TEXT, discharge_at TEXT, status TEXT NOT NULL,
                    updated_at TEXT NOT NULL, UNIQUE(facility, visit_number));
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id),
                    message_id INTEGER NOT NULL REFERENCES messages(id), code TEXT NOT NULL,
                    display TEXT, value TEXT, units TEXT, status TEXT, observed_at TEXT);
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id),
                    message_id INTEGER NOT NULL REFERENCES messages(id), placer_id TEXT,
                    filler_id TEXT, code TEXT NOT NULL, display TEXT, status TEXT, ordered_at TEXT);
                CREATE INDEX IF NOT EXISTS idx_encounters_admit ON encounters(facility, admit_at);
                CREATE INDEX IF NOT EXISTS idx_observations_code ON observations(code, value);
                CREATE INDEX IF NOT EXISTS idx_orders_code ON orders(code);
            """)

    def ingest(self, message: ParsedMessage) -> int:
        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as conn:
            patient_id: int | None = None
            if message.patient:
                p = message.patient
                conn.execute("""INSERT INTO patients
                    (external_id, assigning_authority, family_name, given_name, birth_date, sex, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(external_id, assigning_authority) DO UPDATE SET
                    family_name=COALESCE(excluded.family_name, family_name),
                    given_name=COALESCE(excluded.given_name, given_name),
                    birth_date=COALESCE(excluded.birth_date, birth_date),
                    sex=COALESCE(excluded.sex, sex), updated_at=excluded.updated_at""",
                    (p.external_id, p.assigning_authority or "", p.family_name, p.given_name,
                     p.birth_date, p.sex, now))
                patient_id = conn.execute(
                    "SELECT id FROM patients WHERE external_id=? AND assigning_authority=?",
                    (p.external_id, p.assigning_authority or "")).fetchone()[0]
            try:
                cursor = conn.execute("""INSERT INTO messages
                    (sending_application, sending_facility, control_id, message_type, trigger_event,
                     version, message_at, received_at, patient_id, raw)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (message.sending_application, message.sending_facility, message.control_id,
                     message.message_type, message.trigger_event, message.version,
                     message.message_at.isoformat(), now, patient_id, message.raw))
            except sqlite3.IntegrityError as exc:
                raise DuplicateMessageError(message.control_id) from exc
            message_id = int(cursor.lastrowid)
            if patient_id and message.encounter:
                e = message.encounter
                status = "discharged" if message.trigger_event == "A03" or e.discharge_at else "active"
                conn.execute("""INSERT INTO encounters
                    (patient_id, visit_number, facility, patient_class, location, admit_at,
                     discharge_at, status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(facility, visit_number) DO UPDATE SET
                    patient_id=excluded.patient_id,
                    patient_class=COALESCE(excluded.patient_class, patient_class),
                    location=COALESCE(excluded.location, location),
                    admit_at=COALESCE(excluded.admit_at, admit_at),
                    discharge_at=COALESCE(excluded.discharge_at, discharge_at),
                    status=excluded.status, updated_at=excluded.updated_at""",
                    (patient_id, e.visit_number, message.sending_facility, e.patient_class, e.location,
                     e.admit_at.isoformat() if e.admit_at else None,
                     e.discharge_at.isoformat() if e.discharge_at else None, status, now))
            for o in message.observations:
                if patient_id:
                    conn.execute("""INSERT INTO observations
                        (patient_id, message_id, code, display, value, units, status, observed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (patient_id, message_id, o.code, o.display, o.value, o.units, o.status,
                         o.observed_at.isoformat() if o.observed_at else None))
            for order in message.orders:
                if patient_id:
                    conn.execute("""INSERT INTO orders
                        (patient_id, message_id, placer_id, filler_id, code, display, status, ordered_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (patient_id, message_id, order.placer_id, order.filler_id, order.code,
                         order.display, order.status,
                         order.ordered_at.isoformat() if order.ordered_at else None))
            return message_id

    def search_patients(self, **filters: Any) -> list[dict[str, Any]]:
        clauses, values = ["1=1"], []
        mappings = {
            "facility": "e.facility = ?", "status": "e.status = ?",
            "patient_class": "e.patient_class = ?", "mrn": "p.external_id = ?",
        }
        for key, clause in mappings.items():
            if filters.get(key) is not None:
                clauses.append(clause); values.append(filters[key])
        if filters.get("admitted_since"):
            clauses.append("e.admit_at >= ?"); values.append(filters["admitted_since"])
        if filters.get("admitted_before"):
            clauses.append("e.admit_at < ?"); values.append(filters["admitted_before"])
        if filters.get("observation_code"):
            clauses.append("EXISTS (SELECT 1 FROM observations o WHERE o.patient_id=p.id AND o.code=?)")
            values.append(filters["observation_code"])
        if filters.get("order_code"):
            clauses.append("EXISTS (SELECT 1 FROM orders x WHERE x.patient_id=p.id AND x.code=?)")
            values.append(filters["order_code"])
        numeric = filters.get("observation_min"), filters.get("observation_max")
        if any(value is not None for value in numeric):
            code = filters.get("observation_code")
            if not code:
                raise ValueError("observation_min/max require observation_code")
            bound = []
            if numeric[0] is not None: bound.append("CAST(o.value AS REAL) >= ?"); values.append(numeric[0])
            if numeric[1] is not None: bound.append("CAST(o.value AS REAL) <= ?"); values.append(numeric[1])
            clauses.append("EXISTS (SELECT 1 FROM observations o WHERE o.patient_id=p.id AND o.code=? AND " + " AND ".join(bound) + ")")
            # The extra EXISTS code parameter must precede its bounds.
            values.insert(len(values) - len(bound), code)
        limit = min(max(int(filters.get("limit", 50)), 1), 500)
        sql = f"""SELECT DISTINCT p.*, e.visit_number, e.facility, e.patient_class, e.location,
            e.admit_at, e.discharge_at, e.status FROM patients p
            JOIN encounters e ON e.patient_id=p.id WHERE {' AND '.join(clauses)}
            ORDER BY e.admit_at DESC LIMIT ?"""
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, (*values, limit))]

    def patient_detail(self, patient_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            patient = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
            if not patient:
                return None
            result = dict(patient)
            for table in ("encounters", "observations", "orders"):
                rows = conn.execute(f"SELECT * FROM {table} WHERE patient_id=? ORDER BY id DESC", (patient_id,))
                result[table] = [dict(row) for row in rows]
            return result

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("messages", "patients", "encounters", "observations", "orders")}

