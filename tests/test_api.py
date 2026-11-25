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

