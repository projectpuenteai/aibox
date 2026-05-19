import base64
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


AI_CONTROL_DIR = Path(__file__).resolve().parents[1] / "ai-control"
if str(AI_CONTROL_DIR) not in sys.path:
    sys.path.insert(0, str(AI_CONTROL_DIR))

import app_storage  # type: ignore


@pytest.fixture()
def mounted_app(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "db" / "app.db"))
    monkeypatch.setenv("SESSION_TOKEN_PEPPER", "test-pepper")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_DEFAULT_PASSWORD", "changeme")
    monkeypatch.setenv("APP_ENCRYPTION_MASTER_KEY", base64.b64encode(b"1" * 32).decode("ascii"))
    monkeypatch.setenv("USER_PASSWORD_MIN_LENGTH", "8")
    monkeypatch.setenv("AI_REQUESTS_PER_MIN", "1")
    monkeypatch.setenv("AI_REQUESTS_PER_HOUR", "2")
    monkeypatch.setenv("AI_IP_REQUESTS_PER_MIN", "2")

    monkeypatch.setattr(app_storage.StorageRuntime, "validate_startup_rag", lambda self: None)
    monkeypatch.setattr(app_storage.StorageRuntime, "validate_startup_rag_es", lambda self: None)
    monkeypatch.setattr(app_storage.StorageRuntime, "run_warmup_queries", lambda self: None)
    monkeypatch.setattr(app_storage.StorageRuntime, "cleanup_loop", lambda self: None)
    monkeypatch.setattr(app_storage.StorageRuntime, "keep_warm_loop", lambda self: None)

    app = FastAPI()
    runtime = app_storage.mount_app_storage(app, "http://localhost:2020")
    return app, runtime


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post("/v1/app/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text


def test_signup_rejects_short_user_password(mounted_app):
    app, _runtime = mounted_app
    with TestClient(app) as client:
        response = client.post("/v1/app/auth/signup", json={"username": "shortpass", "password": "abcd"})
    assert response.status_code == 400
    assert "at least 8" in response.text


@pytest.mark.parametrize(
    ("method", "path", "json_payload"),
    [
        ("post", "/v1/app/docs", {"title": "Blocked", "content_markdown": "x"}),
        ("patch", "/v1/app/docs/missing-doc", {"title": "Blocked"}),
        ("delete", "/v1/app/docs/missing-doc", None),
    ],
)
def test_same_origin_write_guard_blocks_cross_site_cookie_write_methods(mounted_app, method, path, json_payload):
    app, _runtime = mounted_app
    with TestClient(app, base_url="http://portal.test") as client:
        response = client.post("/v1/app/auth/signup", json={"username": "writer", "password": "password1"})
        assert response.status_code == 200, response.text
        _login(client, "writer", "password1")
        kwargs = {"headers": {"Origin": "http://evil.test"}}
        if json_payload is not None:
            kwargs["json"] = json_payload
        response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 403


def test_same_origin_write_guard_blocks_sec_fetch_cross_site_without_origin(mounted_app):
    app, _runtime = mounted_app
    with TestClient(app, base_url="http://portal.test") as client:
        response = client.post("/v1/app/auth/signup", json={"username": "fetchsite", "password": "password1"})
        assert response.status_code == 200, response.text
        _login(client, "fetchsite", "password1")
        response = client.post(
            "/v1/app/docs",
            json={"title": "Blocked", "content_markdown": "x"},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
    assert response.status_code == 403


def test_same_origin_write_guard_allows_cookie_write_without_origin_headers(mounted_app):
    app, _runtime = mounted_app
    with TestClient(app, base_url="http://portal.test") as client:
        response = client.post("/v1/app/auth/signup", json={"username": "legacyclient", "password": "password1"})
        assert response.status_code == 200, response.text
        _login(client, "legacyclient", "password1")
        response = client.post("/v1/app/docs", json={"title": "Allowed", "content_markdown": "x"})
    assert response.status_code == 200, response.text


def test_same_origin_write_guard_does_not_block_no_cookie_signup(mounted_app):
    app, _runtime = mounted_app
    with TestClient(app, base_url="http://portal.test") as client:
        response = client.post(
            "/v1/app/auth/signup",
            json={"username": "nocookie", "password": "password1"},
            headers={"Origin": "http://evil.test"},
        )
    assert response.status_code == 200, response.text


def test_same_origin_write_guard_allows_matching_origin(mounted_app):
    app, _runtime = mounted_app
    with TestClient(app, base_url="http://portal.test") as client:
        response = client.post("/v1/app/auth/signup", json={"username": "sameorigin", "password": "password1"})
        assert response.status_code == 200, response.text
        _login(client, "sameorigin", "password1")
        response = client.post(
            "/v1/app/docs",
            json={"title": "Allowed", "content_markdown": "x"},
            headers={"Origin": "http://portal.test"},
        )
    assert response.status_code == 200, response.text


def test_ai_request_rate_limit_records_and_blocks(mounted_app):
    _app, runtime = mounted_app
    with runtime.tx() as c:
        c.execute(
            "INSERT INTO users(id,username,username_norm,password_hash,role,created_at,storage_bytes_used) VALUES(?,?,?,?,?,?,0)",
            ("u1", "user1", "user1", runtime._ph.hash("password1"), "user", runtime.now_iso()),
        )
        runtime.ensure_restrictions_row(c, "u1")
        user = c.execute("SELECT * FROM users WHERE id='u1'").fetchone()
        runtime.check_ai_request_rate(c, user, "127.0.0.1", "/v1/app/chat/completions")
        with pytest.raises(Exception) as excinfo:
            runtime.check_ai_request_rate(c, user, "127.0.0.1", "/v1/app/chat/completions")
    assert getattr(excinfo.value, "status_code", None) == 429


def test_admin_promotion_requires_confirm_and_reason(mounted_app):
    app, _runtime = mounted_app
    with TestClient(app, base_url="http://portal.test") as client:
        response = client.post("/v1/app/auth/signup", json={"username": "promotee", "password": "password1"})
        assert response.status_code == 200, response.text
        target_id = response.json()["user"]["id"]
        _login(client, "admin", "changeme")
        response = client.post(
            f"/v1/app/admin/users/{target_id}/role",
            json={"role": "admin"},
            headers={"Origin": "http://portal.test"},
        )
    assert response.status_code == 400
