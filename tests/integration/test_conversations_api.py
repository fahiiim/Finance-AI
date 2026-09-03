from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import make_service_token


def test_conversation_lifecycle():
    client = TestClient(app)
    token_a = make_service_token("conv-user-A", "conv-session-A")

    create_resp = client.post(
        "/api/v1/conversations",
        headers={"Authorization": f"Bearer {token_a}", "X-Session-Id": "conv-session-A"},
    )
    assert create_resp.status_code == 201

    get_resp = client.get(
        "/api/v1/conversations/conv-session-A",
        headers={"Authorization": f"Bearer {token_a}", "X-Session-Id": "conv-session-A"},
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["user_id"] == "conv-user-A"
    assert get_resp.json()["messages"] == []

    delete_resp = client.delete(
        "/api/v1/conversations/conv-session-A",
        headers={"Authorization": f"Bearer {token_a}", "X-Session-Id": "conv-session-A"},
    )
    assert delete_resp.status_code == 204


def test_a_different_user_cannot_read_someone_elses_session_even_with_same_session_id():
    client = TestClient(app)
    token_a = make_service_token("iso-user-A", "iso-shared-session")
    token_b = make_service_token("iso-user-B", "iso-shared-session")

    client.post(
        "/api/v1/conversations",
        headers={"Authorization": f"Bearer {token_a}", "X-Session-Id": "iso-shared-session"},
    )

    # User B uses the SAME session_id string but a different identity token.
    get_resp = client.get(
        "/api/v1/conversations/iso-shared-session",
        headers={"Authorization": f"Bearer {token_b}", "X-Session-Id": "iso-shared-session"},
    )
    # B has no session of their own yet under (user-B, iso-shared-session) — must be 404, never A's data.
    assert get_resp.status_code == 404


def test_conversation_endpoints_require_authentication():
    client = TestClient(app)
    assert client.post("/api/v1/conversations").status_code == 401
    assert client.get("/api/v1/conversations/some-session").status_code == 401
    assert client.delete("/api/v1/conversations/some-session").status_code == 401


def test_path_session_id_addresses_the_conversation_not_the_header():
    """
    The URL must mean what it says. Previously the path segment was ignored
    and the header's session was returned instead, so requesting session X
    while holding header Y silently served Y's history.
    """
    client = TestClient(app)
    token = make_service_token("path-user", "path-session-1")
    auth = {"Authorization": f"Bearer {token}"}

    # Create two distinct sessions for the SAME user.
    client.post("/api/v1/conversations", headers={**auth, "X-Session-Id": "path-session-1"})
    client.post("/api/v1/conversations", headers={**auth, "X-Session-Id": "path-session-2"})

    # Ask for session 2 while the header still says session 1.
    resp = client.get("/api/v1/conversations/path-session-2", headers={**auth, "X-Session-Id": "path-session-1"})
    assert resp.status_code == 200
    assert resp.json()["session_id"] == "path-session-2"


def test_path_session_id_cannot_reach_another_users_conversation():
    """
    Making the path authoritative must not weaken isolation: user_id still
    comes from the token and is never taken from the URL.
    """
    client = TestClient(app)
    token_a = make_service_token("cross-user-A", "cross-session-A")
    token_b = make_service_token("cross-user-B", "cross-session-B")

    client.post(
        "/api/v1/conversations",
        headers={"Authorization": f"Bearer {token_a}", "X-Session-Id": "cross-session-A"},
    )

    # B names A's session explicitly in the path.
    resp = client.get(
        "/api/v1/conversations/cross-session-A",
        headers={"Authorization": f"Bearer {token_b}", "X-Session-Id": "cross-session-B"},
    )
    assert resp.status_code == 404


def test_delete_only_removes_the_named_session():
    client = TestClient(app)
    token = make_service_token("del-user", "del-session-1")
    auth = {"Authorization": f"Bearer {token}"}

    client.post("/api/v1/conversations", headers={**auth, "X-Session-Id": "del-session-1"})
    client.post("/api/v1/conversations", headers={**auth, "X-Session-Id": "del-session-2"})

    # Delete session 2 while the header names session 1.
    assert client.delete(
        "/api/v1/conversations/del-session-2", headers={**auth, "X-Session-Id": "del-session-1"}
    ).status_code == 204

    # Session 1 must survive.
    assert client.get(
        "/api/v1/conversations/del-session-1", headers={**auth, "X-Session-Id": "del-session-1"}
    ).status_code == 200
    assert client.get(
        "/api/v1/conversations/del-session-2", headers={**auth, "X-Session-Id": "del-session-1"}
    ).status_code == 404
