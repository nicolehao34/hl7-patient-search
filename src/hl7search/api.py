from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .parser import HL7ParseError, parse_message
from .storage import DuplicateMessageError, Store


class MessageFilters(BaseModel):
    sending_application: str | None = None
    sending_facility: str | None = None
    message_since: datetime | None = None
    message_before: datetime | None = None
    ordering_provider_id: str | None = None
    ordering_provider_name: str | None = None
    message_type: str | None = None
    trigger_event: str | None = None
    control_id: str | None = None
    limit: int = Field(50, ge=1, le=500)


class MessageHistoryFilters(MessageFilters):
    patient_id: int | None = Field(None, gt=0)
    before_id: int | None = Field(None, gt=0)


class PatientFilters(MessageFilters):
    facility: str | None = None
    admitted_since: datetime | None = None
    admitted_before: datetime | None = None
    status: str | None = None
    patient_class: str | None = None
    mrn: str | None = None
    observation_code: str | None = None
    observation_min: float | None = None
    observation_max: float | None = None
    order_code: str | None = None


def create_app(database: str | Path | None = None) -> FastAPI:
    store = Store(database or os.getenv("HL7_DATABASE", "hl7.db"))
    app = FastAPI(title="HL7 Patient Search", version="0.1.0")
    app.state.store = store

    @app.get("/", include_in_schema=False)
    def search_ui() -> FileResponse:
        return FileResponse(Path(__file__).with_name("search.html"), media_type="text/html")

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

    @app.get("/messages")
    def messages(filters: Annotated[MessageHistoryFilters, Query()]) -> list[dict[str, object]]:
        try:
            return store.search_messages(**filters.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/messages/{message_id}")
    def message_detail(message_id: int) -> dict[str, object]:
        result = store.message_detail(message_id)
        if result is None:
            raise HTTPException(status_code=404, detail="message not found")
        return result

    @app.get("/patients")
    def patients(filters: Annotated[PatientFilters, Query()]) -> list[dict[str, object]]:
        params = filters.model_dump(exclude_none=True)
        for key in ("admitted_since", "admitted_before"):
            if key in params:
                params[key] = params[key].isoformat()
        try:
            return store.search_patients(**params)
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

