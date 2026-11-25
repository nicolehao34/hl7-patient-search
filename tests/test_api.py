from pathlib import Path

from fastapi.testclient import TestClient

from hl7search.api import create_app
from hl7search.parser import parse_message
from hl7search.storage import Store


def test_http_ingest_search_and_detail(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "api.db"))
    example = Path(__file__).parents[1] / "examples" / "adt_a01_alice.hl7"
    response = client.post("/messages", content=example.read_bytes())
    assert response.status_code == 202

    response = client.get("/patients", params={"facility": "GENERAL", "patient_class": "I"})
    assert response.status_code == 200
    patients = response.json()
    assert [patient["external_id"] for patient in patients] == ["P1001"]

    detail = client.get(f"/patients/{patients[0]['id']}")
    assert detail.status_code == 200
    assert detail.json()["encounters"][0]["visit_number"] == "V1001"


def test_numeric_bound_requires_observation_code(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "api.db"))
    response = client.get("/patients", params={"observation_min": 2})
    assert response.status_code == 422
    assert "require observation_code" in response.json()["detail"]


def result_message(control_id: str, sender: str, code: str) -> str:
    obr = [""] * 17
    obr[0], obr[4], obr[16] = "OBR", "PANEL", "P1^Chen^Maya~P2^Patel^Anika"
    return "\r".join((
        rf"MSH|^~\&|{sender}|GENERAL|||20261125090000.1250-0800||ORU^R01|{control_id}|P|2.5.1",
        "PID|1||TEST^^^GENERAL^MR",
        "|".join(obr),
        f"OBX|1|NM|{code}||1.8",
        "",
    ))


def test_message_history_and_same_message_patient_filters(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "api.db"))
    ids = []
    for control, sender, code in (("TEST-1", "LAB", "2160-0"), ("TEST-2", "OTHER", "718-7")):
        response = client.post("/messages", content=result_message(control, sender, code))
        assert response.status_code == 202
        ids.append(response.json()["message_id"])

    filters = {
        "sending_application": "LAB", "sending_facility": "GENERAL",
        "message_since": "2026-11-25T17:00:00.125Z", "message_before": "2026-11-25T17:00:01Z",
        "ordering_provider_id": "P2", "ordering_provider_name": "anika patel",
    }
    history = client.get("/messages", params=filters).json()
    assert [row["id"] for row in history] == [ids[0]]
    assert "raw" not in history[0]
    detail = client.get(f"/messages/{ids[0]}").json()
    assert detail["raw"] == result_message("TEST-1", "LAB", "2160-0")
    assert detail["ordering_providers"][1] == {"identifier": "P2", "name": "Anika Patel"}
    assert client.get("/messages/9999").status_code == 404
    assert client.get("/messages", params={**filters, "ordering_provider_id": "P1"}).json() == []
    assert client.get("/messages", params={
        "sending_application": "LAB", "message_before": "2026-11-25T17:00:00.125Z",
    }).json() == []
    assert client.get("/messages", params={**filters, "before_id": ids[0]}).json() == []
    assert client.get("/messages", params={**filters, "message_before":
                      filters["message_since"]}).status_code == 422
    assert client.get("/patients", params={**filters, "observation_code": "718-7"}).json() == []
    cohort = client.get("/patients", params={**filters, "observation_code": "2160-0"}).json()
    assert [row["external_id"] for row in cohort] == ["TEST"]
    assert cohort[0]["visit_number"] is None


def test_legacy_messages_backfill_provider_and_timestamp(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    store = Store(db)
    message_id = store.ingest(parse_message(result_message("LEGACY-1", "LAB", "2160-0")))
    with store.connect() as conn:
        conn.execute("DROP TABLE ordering_providers")
        conn.execute("UPDATE messages SET message_at='2026-11-25T09:00:00'")
        conn.execute("PRAGMA user_version=0")
    store = Store(db)
    result = store.search_messages(ordering_provider_id="P2")
    assert [(row["id"], row["message_at"]) for row in result] == [
        (message_id, "2026-11-25T17:00:00.125000"),
    ]
    assert len(Store(db).message_detail(message_id)["ordering_providers"]) == 2


def extra_segment_message() -> str:
    specimen = [""] * 19
    specimen[0], specimen[2], specimen[4] = "SPM", "PL-1&LAB^FL-1&LAB", "BLD^Blood^HL70487"
    specimen[17] = "20261125080000-0800^20261125081500-0800"
    specimen[18] = "20261125083000-0800"
    return result_message("EXTRA-1", "LAB", "2160-0") + "\r".join((
        "EVN|A08|20261125090000-0800||01||20261125084500-0800|GENERAL",
        "ORC|NW|PLACER^EHR|FILLER^LAB||SC||||20261125090000-0800",
        "ORC|CA|OTHER|||CA",
        "|".join(specimen),
        r"NTE|1|L|Follow-up needed\F\review~Repeat collection",
        "NTE|2|P|Other note",
        "DG1|1||A10^One^LOCAL||20261125090000-0800|F",
        "DG1|2||B20^Two^ICD10||20261125090000-0800|W",
        "",
    ))


def test_extra_segments_are_searchable_on_both_endpoints(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "api.db"))
    result = client.post("/messages", content=extra_segment_message())
    assert result.status_code == 202
    message_id = result.json()["message_id"]
    assert client.post("/messages", content=extra_segment_message()).json()["duplicate"]
    other = client.post("/messages", content=result_message("EXTRA-2", "OTHER", "718-7"))
    assert other.status_code == 202
    filters = {
        "event_code": "A08", "event_reason": "01", "event_recorded_since": "2026-11-25T17:00:00Z",
        "event_recorded_before": "2026-11-25T17:01:00Z", "order_control": "NW",
        "order_status": "SC", "placer_order_id": "PLACER", "filler_order_id": "FILLER",
        "specimen_id": "FL-1", "specimen_type": "BLD", "note_source": "L",
        "note_contains": "NEEDED|review", "diagnosis_code": "A10",
        "diagnosis_system": "LOCAL", "diagnosis_type": "F",
    }
    history = client.get("/messages", params=filters)
    assert history.status_code == 200
    assert [row["id"] for row in history.json()] == [message_id]
    cohort = client.get("/patients", params={**filters, "observation_code": "2160-0"}).json()
    assert [row["external_id"] for row in cohort] == ["TEST"]
    assert client.get("/patients", params={**filters, "observation_code": "718-7"}).json() == []
    assert client.get("/messages", params={**filters, "diagnosis_system": "ICD10"}).json() == []
    assert client.get("/messages", params={**filters, "note_source": "P"}).json() == []
    assert client.get("/messages", params={**filters, "order_status": "CA"}).json() == []
    assert client.get("/messages", params={
        **filters, "event_recorded_before": "2026-11-25T17:00:00Z",
    }).status_code == 422

    details = client.get(f"/messages/{message_id}").json()["segments"]
    assert [item["segment"] for item in details] == [
        "EVN", "ORC", "ORC", "SPM", "NTE", "NTE", "DG1", "DG1",
    ]
    assert details[3]["fields"]["specimen_collection_end"] == "2026-11-25T16:15:00"
    assert details[4]["fields"]["note_text"] == "Follow-up needed|review\nRepeat collection"


def test_extra_segment_migration_backfills_existing_messages_once(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    store = Store(db)
    message_id = store.ingest(parse_message(extra_segment_message()))
    with store.connect() as conn:
        conn.execute("DROP TABLE message_segments")
        conn.execute("PRAGMA user_version=1")
    store = Store(db)
    assert [row["id"] for row in store.search_messages(diagnosis_code="B20")] == [message_id]
    detail = Store(db).message_detail(message_id)
    assert detail is not None
    assert len(detail["segments"]) == 8
    assert len(detail["ordering_providers"]) == 2

