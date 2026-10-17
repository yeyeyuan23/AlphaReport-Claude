"""Only the bot's reading service remains; old task/chat APIs are retired."""

from fastapi.testclient import TestClient


def test_old_chat_and_collection_interfaces_are_unavailable():
    from reportagent.main import app

    client = TestClient(app)
    assert client.get("/api/v1/collection/tasks/list").status_code == 404
    assert client.post("/api/v1/chat", json={}).status_code in {404, 405}
    assert "/api/v1/chat" not in app.openapi()["paths"]
    assert "/api/v1/reports/{report_id}/pdf" in app.openapi()["paths"]
