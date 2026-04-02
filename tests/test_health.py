from fastapi.testclient import TestClient

from union_ledger.main import app

client = TestClient(app)


def test_health_endpoint_returns_ok() -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
