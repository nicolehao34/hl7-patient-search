from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, status

from .parser import HL7ParseError, parse_message
from .storage import DuplicateMessageError, Store


def create_app(database: str | Path | None = None) -> FastAPI:
    store = Store(database or os.getenv("HL7_DATABASE", "hl7.db"))
    app = FastAPI(title="HL7 Patient Search", version="0.1.0")
    app.state.store = store

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "counts": store.stats()}

    @app.post("/messages", status_code=status.HTTP_202_ACCEPTED)
    async def ingest_http(request: Request) -> dict[str, object]:
        try:
            message = parse_message(await request.body())
            message_id = store.ingest(message)
            return {"accepted": True, "message_id": message_id, "control_id": message.control_id}
        except DuplicateMessageError as exc:
            return {"accepted": True, "duplicate": True, "control_id": str(exc)}
        except HL7ParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/patients")
    def patients(
        facility: str | None = None, admitted_since: datetime | None = None,
        admitted_before: datetime | None = None, status_: str | None = Query(None, alias="status"),
        patient_class: str | None = None, mrn: str | None = None,
        observation_code: str | None = None, observation_min: float | None = None,
        observation_max: float | None = None, order_code: str | None = None,
        limit: int = Query(50, ge=1, le=500),
    ) -> list[dict[str, object]]:
        try:
            return store.search_patients(
                facility=facility, admitted_since=admitted_since.isoformat() if admitted_since else None,
                admitted_before=admitted_before.isoformat() if admitted_before else None,
                status=status_, patient_class=patient_class, mrn=mrn,
                observation_code=observation_code, observation_min=observation_min,
                observation_max=observation_max, order_code=order_code, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/patients/{patient_id}")
    def patient(patient_id: int) -> dict[str, object]:
        result = store.patient_detail(patient_id)
        if result is None:
            raise HTTPException(status_code=404, detail="patient not found")
        return result

    return app


app = create_app()

