from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Patient:
    external_id: str
    assigning_authority: str | None
    family_name: str | None
    given_name: str | None
    birth_date: str | None
    sex: str | None


@dataclass(frozen=True, slots=True)
class Encounter:
    visit_number: str
    patient_class: str | None
    location: str | None
    admit_at: datetime | None
    discharge_at: datetime | None


@dataclass(frozen=True, slots=True)
class Observation:
    code: str
    display: str | None
    value: str | None
    units: str | None
    status: str | None
    observed_at: datetime | None


@dataclass(frozen=True, slots=True)
class Order:
    placer_id: str | None
    filler_id: str | None
    code: str
    display: str | None
    status: str | None
    ordered_at: datetime | None


@dataclass(frozen=True, slots=True)
class OrderingProvider:
    identifier: str | None
    name: str | None


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    raw: str
    sending_application: str
    sending_facility: str
    message_at: datetime
    message_type: str
    trigger_event: str
    control_id: str
    version: str
    patient: Patient | None
    encounter: Encounter | None
    observations: tuple[Observation, ...] = field(default_factory=tuple)
    orders: tuple[Order, ...] = field(default_factory=tuple)
    ordering_providers: tuple[OrderingProvider, ...] = field(default_factory=tuple)

