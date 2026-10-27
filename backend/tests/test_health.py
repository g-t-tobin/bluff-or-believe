from fastapi.testclient import TestClient

from app.main import app, room_manager
from app.room_repository import RoomRepositoryError

client = TestClient(app)


def test_health_endpoint_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_endpoint_checks_storage_without_changing_room_state(
    monkeypatch,
) -> None:
    repository = room_manager.repository
    original_load = repository.load
    calls = []

    def track_load(room_id):
        calls.append(room_id)
        return original_load(room_id)

    monkeypatch.setattr(repository, "load", track_load)
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert calls == ["__readiness_probe__"]


def test_readiness_endpoint_reports_storage_unavailable(monkeypatch) -> None:
    def fail_load(_room_id):
        raise RoomRepositoryError("storage unavailable")

    monkeypatch.setattr(room_manager.repository, "load", fail_load)
    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "Room storage is unavailable."}
