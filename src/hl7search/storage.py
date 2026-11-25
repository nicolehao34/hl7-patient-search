from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import ParsedMessage
from .parser import HL7ParseError, parse_message, utc_message_time

SEGMENT_FILTERS = {
    "EVN": {
        "event_code": "json_extract(s.fields, '$.event_code') = ?",
        "event_reason": "json_extract(s.fields, '$.event_reason') = ?",
        "event_recorded_since": "json_extract(s.fields, '$.event_recorded_at') >= ?",
        "event_recorded_before": "json_extract(s.fields, '$.event_recorded_at') < ?",
    },
    "ORC": {
        "order_control": "json_extract(s.fields, '$.order_control') = ?",
        "order_status": "json_extract(s.fields, '$.order_status') = ?",
        "placer_order_id": "json_extract(s.fields, '$.placer_order_id') = ?",
        "filler_order_id": "json_extract(s.fields, '$.filler_order_id') = ?",
    },
    "SPM": {
        "specimen_type": "json_extract(s.fields, '$.specimen_type') = ?",
        "specimen_id": "? IN (json_extract(s.fields, '$.placer_specimen_id'), "
                       "json_extract(s.fields, '$.filler_specimen_id'))",
    },
    "NTE": {
        "note_source": "json_extract(s.fields, '$.note_source') = ?",
        "note_contains": "instr(lower(json_extract(s.fields, '$.note_text')), lower(?)) > 0",
    },
    "DG1": {
        "diagnosis_code": "json_extract(s.fields, '$.diagnosis_code') = ?",
        "diagnosis_system": "json_extract(s.fields, '$.diagnosis_system') = ?",
        "diagnosis_type": "json_extract(s.fields, '$.diagnosis_type') = ?",
    },
}


def message_predicates(filters: dict[str, Any]) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    for key in ("sending_application", "sending_facility", "message_type", "trigger_event", "control_id"):
        if filters.get(key) is not None:
            clauses.append(f"m.{key} = ?")
            values.append(filters[key])
    since, before = (
        utc_message_time(filters[key]) if filters.get(key) is not None else None
        for key in ("message_since", "message_before")
    )
    if since is not None and before is not None and since >= before:
        raise ValueError("message_since must be earlier than message_before")
    if since is not None:
        clauses.append("m.message_at >= ?")
        values.append(since.isoformat())
    if before is not None:
        clauses.append("m.message_at < ?")
        values.append(before.isoformat())
    provider = ["op.message_id = m.id"]
    for key, clause in (
        ("ordering_provider_id", "op.identifier = ?"),
        ("ordering_provider_name", "op.name = ? COLLATE NOCASE"),
    ):
        if filters.get(key) is not None:
            provider.append(clause)
            values.append(filters[key])
    if len(provider) > 1:
        clauses.append(f"EXISTS (SELECT 1 FROM ordering_providers op WHERE {' AND '.join(provider)})")
    for key in ("event_recorded_since", "event_recorded_before"):
        if filters.get(key) is not None:
            filters[key] = utc_message_time(filters[key]).isoformat()
    event_since = filters.get("event_recorded_since")
    event_before = filters.get("event_recorded_before")
    if event_since is not None and event_before is not None and event_since >= event_before:
        raise ValueError("event_recorded_since must be earlier than event_recorded_before")
    for segment, conditions in SEGMENT_FILTERS.items():
        selected = [key for key in conditions if filters.get(key) is not None]
        if selected:
            matches = " AND ".join(conditions[key] for key in selected)
            clauses.append(
                f"EXISTS (SELECT 1 FROM message_segments s "
                f"WHERE s.message_id=m.id AND s.segment=? AND {matches})"
            )
            values.extend([segment, *(filters[key] for key in selected)])
    return clauses, values


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
                CREATE TABLE IF NOT EXISTS ordering_providers (
                    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    identifier TEXT, name TEXT);
                CREATE INDEX IF NOT EXISTS idx_provider_message ON ordering_providers(message_id);
                CREATE INDEX IF NOT EXISTS idx_provider_identifier ON ordering_providers(identifier, message_id);
                CREATE INDEX IF NOT EXISTS idx_provider_name ON ordering_providers(name COLLATE NOCASE, message_id);
                CREATE INDEX IF NOT EXISTS idx_message_source_time ON messages(sending_application, sending_facility, message_at);
                CREATE INDEX IF NOT EXISTS idx_message_facility_time ON messages(sending_facility, message_at);
                CREATE INDEX IF NOT EXISTS idx_message_time ON messages(message_at);
                CREATE INDEX IF NOT EXISTS idx_message_patient ON messages(patient_id, id);
                CREATE INDEX IF NOT EXISTS idx_observations_message_code ON observations(message_id, code);
                CREATE INDEX IF NOT EXISTS idx_orders_message_code ON orders(message_id, code);
                CREATE TABLE IF NOT EXISTS message_segments (
                    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL, segment TEXT NOT NULL, fields TEXT NOT NULL,
                    PRIMARY KEY (message_id, position));
                CREATE INDEX IF NOT EXISTS idx_message_segments_kind
                    ON message_segments(message_id, segment);
            """)
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < 2:
                for row in conn.execute("SELECT id, raw, message_at FROM messages"):
                    try:
                        message = parse_message(row["raw"])
                    except HL7ParseError:
                        continue
                    if version == 0:
                        timestamp = message.message_at.isoformat()
                        if timestamp != row["message_at"]:
                            conn.execute(
                                "UPDATE messages SET message_at=? WHERE id=?",
                                (timestamp, row["id"]),
                            )
                        self._insert_providers(conn, row["id"], message)
                    self._insert_search_segments(conn, row["id"], message)
                conn.execute("PRAGMA user_version=2")

    @staticmethod
    def _insert_providers(conn: sqlite3.Connection, message_id: int, message: ParsedMessage) -> None:
        conn.executemany(
            "INSERT INTO ordering_providers (message_id, identifier, name) VALUES (?, ?, ?)",
            ((message_id, provider.identifier, provider.name) for provider in message.ordering_providers),
        )

    @staticmethod
    def _insert_search_segments(
        conn: sqlite3.Connection, message_id: int, message: ParsedMessage,
    ) -> None:
        conn.executemany(
            "INSERT INTO message_segments (message_id, position, segment, fields) VALUES (?, ?, ?, ?)",
            ((message_id, item.position, item.segment, json.dumps(item.fields))
             for item in message.search_segments),
        )

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
            self._insert_providers(conn, message_id, message)
            self._insert_search_segments(conn, message_id, message)
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
        source_clauses, source_values = message_predicates(filters)
        scope = "message_id=m.id" if source_clauses else "patient_id=p.id"
        clinical_clauses: list[str] = []
        clinical_values: list[Any] = []
        code = filters.get("observation_code")
        bounds = filters.get("observation_min"), filters.get("observation_max")
        if any(value is not None for value in bounds) and not code:
            raise ValueError("observation_min/max require observation_code")
        if code:
            observation = [scope, "code=?"]
            clinical_values.append(code)
            for op, value in zip((">=", "<="), bounds, strict=True):
                if value is not None:
                    observation.append(f"CAST(value AS REAL) {op} ?")
                    clinical_values.append(value)
            clinical_clauses.append(
                f"EXISTS (SELECT 1 FROM observations WHERE {' AND '.join(observation)})"
            )
        if filters.get("order_code"):
            clinical_clauses.append(f"EXISTS (SELECT 1 FROM orders WHERE {scope} AND code=?)")
            clinical_values.append(filters["order_code"])
        if source_clauses:
            match = " AND ".join(["m.patient_id=p.id", *source_clauses, *clinical_clauses])
            clauses.append(f"EXISTS (SELECT 1 FROM messages m WHERE {match})")
        else:
            clauses.extend(clinical_clauses)
        values.extend(source_values)
        values.extend(clinical_values)
        limit = min(max(int(filters.get("limit", 50)), 1), 500)
        join = "LEFT JOIN" if source_clauses else "JOIN"
        sql = f"""SELECT DISTINCT p.*, e.visit_number, e.facility, e.patient_class, e.location,
            e.admit_at, e.discharge_at, e.status FROM patients p
            {join} encounters e ON e.patient_id=p.id WHERE {' AND '.join(clauses)}
            ORDER BY e.admit_at DESC LIMIT ?"""
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, (*values, limit))]

    def search_messages(self, **filters: Any) -> list[dict[str, Any]]:
        clauses, values = message_predicates(filters)
        for key, column in (("patient_id", "m.patient_id = ?"), ("before_id", "m.id < ?")):
            if filters.get(key) is not None:
                clauses.append(column)
                values.append(filters[key])
        where = " AND ".join(clauses) or "1=1"
        with self.connect() as conn:
            rows = conn.execute(f"""
                SELECT m.id, m.patient_id, m.sending_application, m.sending_facility, m.control_id,
                       m.message_type, m.trigger_event, m.version, m.message_at, m.received_at
                FROM messages m WHERE {where} ORDER BY m.id DESC LIMIT ?
                """, (*values, filters.get("limit", 50)))
            return [dict(row) for row in rows]

    def message_detail(self, message_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None:
                return None
            providers = conn.execute(
                "SELECT identifier, name FROM ordering_providers WHERE message_id=? ORDER BY rowid",
                (message_id,),
            )
            segments = conn.execute(
                "SELECT position, segment, fields FROM message_segments "
                "WHERE message_id=? ORDER BY position",
                (message_id,),
            )
            return {
                **dict(row),
                "ordering_providers": [dict(provider) for provider in providers],
                "segments": [
                    {"position": item["position"], "segment": item["segment"],
                     "fields": json.loads(item["fields"])}
                    for item in segments
                ],
            }

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

