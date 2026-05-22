import base64
import sys
from pathlib import Path
from urllib.parse import quote

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
    monkeypatch.setenv("APP_ENCRYPTION_MASTER_KEY", base64.b64encode(b"0" * 32).decode("ascii"))

    def fake_validate_startup_rag(self):
        self._update_rag_status(
            startup_rag_ok=True,
            startup_rag_error=None,
            startup_rag_checked_at=self.now_iso(),
            startup_reranker_ok=False,
            startup_reranker_error=None,
        )

    monkeypatch.setattr(app_storage.StorageRuntime, "validate_startup_rag", fake_validate_startup_rag)
    monkeypatch.setattr(app_storage.StorageRuntime, "run_warmup_queries", lambda self: None)
    monkeypatch.setattr(app_storage.StorageRuntime, "cleanup_loop", lambda self: None)
    monkeypatch.setattr(app_storage.StorageRuntime, "keep_warm_loop", lambda self: None)

    app = FastAPI()
    runtime = app_storage.mount_app_storage(app, "http://localhost:2020")
    return app, runtime


def _signup_and_login(client: TestClient, username: str, language: str) -> None:
    response = client.post(
        "/v1/app/auth/signup",
        json={
            "username": username,
            "password": "pass1234",
            "preferred_language": language,
        },
    )
    assert response.status_code == 200, response.text
    response = client.post(
        "/v1/app/auth/login",
        json={
            "username": username,
            "password": "pass1234",
        },
    )
    assert response.status_code == 200, response.text


def test_wiki_root_redirects_anonymous_to_english(mounted_app):
    app, _runtime = mounted_app
    with TestClient(app) as client:
        response = client.get("/wiki/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/wiki/en/"


def test_wiki_root_redirects_english_user_to_english_zim(mounted_app):
    app, _runtime = mounted_app
    with TestClient(app) as client:
        _signup_and_login(client, "english-user", "en")
        response = client.get("/wiki/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/wiki/en/"


def test_wiki_root_redirects_spanish_user_to_spanish_zim(mounted_app):
    app, _runtime = mounted_app
    with TestClient(app) as client:
        _signup_and_login(client, "spanish-user", "es")
        response = client.get("/wiki/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/wiki/es/"


def test_wiki_article_redirect_preserves_path_and_query_for_spanish_user(mounted_app):
    app, _runtime = mounted_app
    with TestClient(app) as client:
        _signup_and_login(client, "spanish-path-user", "es")
        response = client.get("/wiki/viewer?search=energia", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/wiki/es/viewer?search=energia"


def test_language_specific_citation_uses_requested_language(mounted_app):
    _app, runtime = mounted_app
    citation = runtime._build_wiki_citation("Cafe", "http://localhost", wiki_language="es")
    assert citation is not None
    assert citation["wiki_url"] == (
        "http://localhost/wiki/es/search"
        f"?books.name={quote(runtime.kiwix_book_es, safe='')}&pattern=Cafe"
    )


def test_citations_follow_rag_index_language_when_user_prefers_spanish(mounted_app):
    _app, runtime = mounted_app
    citations = runtime._citations_from_chunks(
        [
            {"title": "Photosynthesis", "rag_index_language": "en"},
            {"title": "Fotosintesis", "rag_index_language": "es"},
        ],
        "http://localhost",
    )
    urls = [citation["wiki_url"] for citation in citations]
    en_book = quote(runtime.kiwix_book_en, safe="")
    es_book = quote(runtime.kiwix_book_es, safe="")
    assert urls == [
        f"http://localhost/wiki/en/search?books.name={en_book}&pattern=Photosynthesis",
        f"http://localhost/wiki/es/search?books.name={es_book}&pattern=Fotosintesis",
    ]
