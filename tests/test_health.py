from fastapi.testclient import TestClient

from union_ledger.main import app

client = TestClient(app)


def test_health_check() -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_system_metadata() -> None:
    response = client.get("/api/v1/system/metadata")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "Union Ledger API"
    assert body["api_prefix"] == "/api/v1"

