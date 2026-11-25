from __future__ import annotations

import re
from datetime import UTC, datetime

from .models import Encounter, Observation, Order, OrderingProvider, ParsedMessage, Patient


class HL7ParseError(ValueError):
    """The wire message is not a minimally valid HL7 v2 message."""


def parse_timestamp(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    # HL7 DTM permits decreasing precision and an optional timezone.
    for fmt, length in (("%Y%m%d%H%M%S", 14), ("%Y%m%d%H%M", 12), ("%Y%m%d%H", 10), ("%Y%m%d", 8)):
        try:
            return datetime.strptime(value[:length], fmt)
        except ValueError:
            continue
    raise HL7ParseError(f"invalid HL7 timestamp: {value!r}")


def utc_message_time(value: datetime) -> datetime:
    """Use a common UTC timeline for offset-bearing and offset-free messages."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def parse_message_timestamp(value: str) -> datetime:
    if not value:
        raise HL7ParseError("MSH-7 message timestamp is required")
    match = re.fullmatch(r"([0-9]{4}(?:[0-9]{2}){0,5})(\.[0-9]{1,4})?([+-][0-9]{4})?", value)
    if match is None:
        raise HL7ParseError("invalid MSH-7 timestamp")
    digits, fraction, offset = match.groups()
    formats = {4: "%Y", 6: "%Y%m", 8: "%Y%m%d", 10: "%Y%m%d%H",
               12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}
    if fraction and len(digits) != 14:
        raise HL7ParseError("invalid MSH-7 timestamp precision")
    fmt = formats[len(digits)] + (".%f" if fraction else "") + ("%z" if offset else "")
    try:
        return utc_message_time(datetime.strptime(value, fmt))
    except (ValueError, OverflowError) as exc:
        raise HL7ParseError("invalid MSH-7 timestamp") from exc


def _component(value: str, index: int, component_sep: str = "^") -> str | None:
    parts = value.split(component_sep)
    return parts[index] or None if index < len(parts) else None


def _unescape(value: str | None, escape: str = "\\") -> str | None:
    if value is None:
        return None
    replacements = {"F": "|", "S": "^", "R": "~", "T": "&", "E": escape, ".br": "\n"}
    for code, replacement in replacements.items():
        value = value.replace(f"{escape}{code}{escape}", replacement)
    return value


def parse_message(raw: str | bytes) -> ParsedMessage:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HL7ParseError("message is not valid UTF-8") from exc
    raw = raw.strip("\x0b\x1c\r\n")
    normalized = raw.replace("\r\n", "\r").replace("\n", "\r")
    lines = [line for line in normalized.split("\r") if line]
    if not lines or not lines[0].startswith("MSH") or len(lines[0]) < 8:
        raise HL7ParseError("message must begin with an MSH segment")

    field_sep = lines[0][3]
    msh = lines[0].split(field_sep)
    # MSH-1 is the separator itself, so split index 1 is MSH-2 and index 11 is MSH-12.
    if len(msh) < 12:
        raise HL7ParseError("MSH is missing required fields")
    encoding = msh[1]
    component_sep = encoding[0] if encoding else "^"
    repetition_sep = encoding[1] if len(encoding) > 1 else "~"
    escape = encoding[2] if len(encoding) > 2 else "\\"
    subcomponent_sep = encoding[3] if len(encoding) > 3 else "&"
    segments: dict[str, list[list[str]]] = {}
    for line in lines:
        fields = line.split(field_sep)
        segments.setdefault(fields[0], []).append(fields)

    def field(segment: str, number: int, repetition: int = 0) -> str:
        group = segments.get(segment)
        index = number - 1 if segment == "MSH" else number
        if not group or index >= len(group[repetition]):
            return ""
        return group[repetition][index]

    msg = field("MSH", 9).split(component_sep)
    if len(msg) < 2 or not msg[0] or not msg[1]:
        raise HL7ParseError("MSH-9 must contain message type and trigger event")
    control_id = field("MSH", 10)
    if not control_id:
        raise HL7ParseError("MSH-10 message control ID is required")

    patient = None
    if "PID" in segments:
        identifiers = field("PID", 3).split("~")
        chosen = next((item for item in identifiers if item.split(component_sep)[0]), "")
        external_id = _component(chosen, 0, component_sep)
        if not external_id:
            raise HL7ParseError("PID-3 patient identifier is required when PID is present")
        name = field("PID", 5).split("~")[0]
        patient = Patient(
            external_id=_unescape(external_id, escape) or "",
            assigning_authority=_unescape(_component(chosen, 3, component_sep), escape),
            family_name=_unescape(_component(name, 0, component_sep), escape),
            given_name=_unescape(_component(name, 1, component_sep), escape),
            birth_date=field("PID", 7)[:8] or None,
            sex=field("PID", 8) or None,
        )

    encounter = None
    if "PV1" in segments:
        visit = field("PV1", 19)
        visit_number = _component(visit, 0, component_sep) or f"{control_id}:visit"
        location_parts = field("PV1", 3).split(component_sep)
        location = " / ".join(part for part in location_parts[:3] if part) or None
        encounter = Encounter(
            visit_number=visit_number,
            patient_class=field("PV1", 2) or None,
            location=location,
            admit_at=parse_timestamp(field("PV1", 44)),
            discharge_at=parse_timestamp(field("PV1", 45)),
        )

    obr_time = parse_timestamp(field("OBR", 7)) if "OBR" in segments else None
    observations = []
    for obx in segments.get("OBX", []):
        value = obx[5] if len(obx) > 5 else ""
        code = obx[3].split(component_sep) if len(obx) > 3 else []
        observations.append(Observation(
            code=code[0] if code else "unknown",
            display=_unescape(code[1], escape) if len(code) > 1 else None,
            value=_unescape(value, escape),
            units=_unescape(obx[6], escape) if len(obx) > 6 else None,
            status=obx[11] if len(obx) > 11 and obx[11] else None,
            observed_at=parse_timestamp(obx[14]) if len(obx) > 14 and obx[14] else obr_time,
        ))

    orders = []
    for obr in segments.get("OBR", []) if msg[0] in {"ORM", "OMG", "OML"} else []:
        code = obr[4].split(component_sep) if len(obr) > 4 else []
        if code:
            orders.append(Order(
                placer_id=_component(obr[2], 0, component_sep) if len(obr) > 2 else None,
                filler_id=_component(obr[3], 0, component_sep) if len(obr) > 3 else None,
                code=code[0],
                display=_unescape(code[1], escape) if len(code) > 1 else None,
                status=obr[25] if len(obr) > 25 and obr[25] else None,
                ordered_at=parse_timestamp(obr[7]) if len(obr) > 7 and obr[7] else None,
            ))

    providers: list[OrderingProvider] = []
    for obr in segments.get("OBR", []):
        if len(obr) <= 16:
            continue
        for xcn in obr[16].split(repetition_sep):
            identifier = _unescape(_component(xcn, 0, component_sep), escape)
            family = _component(xcn, 1, component_sep) or ""
            given = _component(xcn, 2, component_sep)
            surname = _unescape(family.split(subcomponent_sep)[0], escape)
            name = " ".join(part for part in (_unescape(given, escape), surname) if part)
            if identifier or name:
                providers.append(OrderingProvider(identifier, name or None))

    message_at = parse_message_timestamp(field("MSH", 7).split(component_sep)[0])
    return ParsedMessage(
        raw=normalized + "\r",
        sending_application=field("MSH", 3), sending_facility=field("MSH", 4),
        message_at=message_at, message_type=msg[0], trigger_event=msg[1],
        control_id=control_id, version=field("MSH", 12), patient=patient,
        encounter=encounter, observations=tuple(observations), orders=tuple(orders),
        ordering_providers=tuple(providers),
    )

