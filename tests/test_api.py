from pathlib import Path

from fastapi.testclient import TestClient

from hl7search.api import create_app


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

