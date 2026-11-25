from pathlib import Path

import pytest

from hl7search.parser import parse_message
from hl7search.storage import DuplicateMessageError, Store

EXAMPLES = Path(__file__).parents[1] / "examples"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    result = Store(tmp_path / "test.db")
    for path in EXAMPLES.glob("*.hl7"):
        result.ingest(parse_message(path.read_text()))
    return result


def test_recent_inpatients_at_facility(store: Store) -> None:
    rows = store.search_patients(
        facility="GENERAL", admitted_since="2026-11-25T00:00:00", patient_class="I")
    assert [row["external_id"] for row in rows] == ["P1001"]


def test_lab_threshold_selects_alice(store: Store) -> None:
    rows = store.search_patients(
        observation_code="2160-0", observation_min=1.5, status="active")
    assert [row["external_id"] for row in rows] == ["P1001"]


def test_order_selects_carol(store: Store) -> None:
    rows = store.search_patients(order_code="IMG200")
    assert [row["external_id"] for row in rows] == ["P3003"]


def test_ingestion_is_idempotent(store: Store) -> None:
    message = parse_message((EXAMPLES / "adt_a01_alice.hl7").read_text())
    with pytest.raises(DuplicateMessageError):
        store.ingest(message)

