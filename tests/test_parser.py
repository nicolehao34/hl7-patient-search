from pathlib import Path

import pytest

from hl7search.parser import HL7ParseError, parse_message

EXAMPLES = Path(__file__).parents[1] / "examples"


def test_parses_adt_fields() -> None:
    message = parse_message((EXAMPLES / "adt_a01_alice.hl7").read_text())
    assert (message.message_type, message.trigger_event) == ("ADT", "A01")
    assert message.patient and message.patient.external_id == "P1001"
    assert message.patient.family_name == "Nguyen"
    assert message.encounter and message.encounter.visit_number == "V1001"
    assert message.encounter.location == "5E / 501 / A"


def test_parses_observations_and_orders() -> None:
    result = parse_message((EXAMPLES / "oru_r01_alice.hl7").read_text())
    assert [(o.code, o.value) for o in result.observations] == [
        ("2160-0", "1.8"), ("718-7", "11.2")]
    order = parse_message((EXAMPLES / "orm_o01_carol.hl7").read_text()).orders[0]
    assert (order.code, order.placer_id) == ("IMG200", "PLACER-100")


def test_rejects_missing_control_id() -> None:
    with pytest.raises(HL7ParseError, match="control ID"):
        parse_message("MSH|^~\\&|A|B|||20261125120000||ADT^A01||P|2.5.1\r")

