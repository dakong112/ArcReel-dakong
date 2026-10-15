"""new-api 统一账号登录：状态接口、登录码交换与首登自动配置供应商。"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import server.auth as auth_module
from lib.db import get_async_session
from lib.db.repositories.custom_provider_repo import CustomProviderRepository
from server.routers import auth as auth_router

NEWAPI = "https://newapi.test"
EXCHANGE_URL = f"{NEWAPI}/api/sso/exchange"
REDIRECT_URI = "https://alice.arcreel.test/sso/callback"


def grant(user_id: int = 7, api_key: str = "sk-alice") -> dict:
    return {
        "success": True,
        "message": "",
        "data": {
            "user": {"id": user_id, "username": "alice", "display_name": "Alice", "role": 1},
            "access_token": "pat-x",
            "access_token_expires_at": 0,
            "api_key": api_key,
            "permissions": [],
        },
    }


@pytest.fixture
def newapi_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN_SECRET", "test-newapi-secret-key-at-least-32-bytes-long")
    monkeypatch.setenv("NEWAPI_BASE_URL", f"{NEWAPI}/")
    monkeypatch.setenv("NEWAPI_PUBLIC_URL", "https://account.test")
    monkeypatch.setenv("NEWAPI_CLIENT_ID", "arcreel-alice")
    monkeypatch.setenv("NEWAPI_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("NEWAPI_REDIRECT_URI", REDIRECT_URI)
    monkeypatch.delenv("NEWAPI_ALLOWED_USER_IDS", raising=False)
    monkeypatch.delenv("NEWAPI_AUTO_PROVIDER", raising=False)


@pytest.fixture
def newapi_client(db_factory: async_sessionmaker[AsyncSession]) -> Iterator[TestClient]:
    app = FastAPI()

    async def override_session():
        async with db_factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = override_session
    app.include_router(auth_router.public_router, prefix="/api/v1")
    with TestClient(app) as c:
        yield c


def exchange(client: TestClient, code: str = "code-1") -> httpx.Response:
    return client.post("/api/v1/auth/newapi/exchange", json={"code": code, "state": "ignored"})


class TestStatus:
    def test_disabled_without_config(self, newapi_client: TestClient, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("NEWAPI_CLIENT_SECRET", raising=False)
        body = newapi_client.get("/api/v1/auth/status").json()
        assert body["newapi"] == {"enabled": False, "authorize_url": None}

    @pytest.mark.usefixtures("newapi_env")
    def test_exposes_authorize_url_on_public_origin(self, newapi_client: TestClient):
        body = newapi_client.get("/api/v1/auth/status").json()
        url = httpx.URL(body["newapi"]["authorize_url"])
        assert body["newapi"]["enabled"] is True
        assert str(url.copy_with(query=None)) == "https://account.test/sso/authorize"
        assert url.params["client_id"] == "arcreel-alice"
        assert url.params["redirect_uri"] == REDIRECT_URI
        assert "state" not in url.params


@pytest.mark.usefixtures("newapi_env")
class TestExchange:
    def test_success_issues_token_for_newapi_subject(self, newapi_client: TestClient):
        with respx.mock(assert_all_called=True) as remote:
            route = remote.post(EXCHANGE_URL).respond(200, json=grant())
            resp = exchange(newapi_client)

        assert resp.status_code == 200, resp.text
        payload = auth_module.verify_token(resp.json()["access_token"])
        assert payload is not None
        assert payload["sub"] == "newapi:7"
        assert json.loads(route.calls.last.request.content) == {
            "client_id": "arcreel-alice",
            "client_secret": "s3cret",
            "code": "code-1",
            "redirect_uri": REDIRECT_URI,
        }

    def test_disabled_returns_404(self, newapi_client: TestClient, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("NEWAPI_BASE_URL")
        with respx.mock(assert_all_called=False) as remote:
            route = remote.post(EXCHANGE_URL)
            resp = exchange(newapi_client)
        assert resp.status_code == 404
        assert not route.called

    @pytest.mark.parametrize(
        ("upstream_status", "expected"),
        [(400, 401), (403, 403), (409, 502), (500, 502)],
    )
    def test_upstream_rejection_is_mapped(self, newapi_client: TestClient, upstream_status: int, expected: int):
        with respx.mock(assert_all_called=True) as remote:
            remote.post(EXCHANGE_URL).respond(
                upstream_status, json={"success": False, "message": "invalid or expired code"}
            )
            resp = exchange(newapi_client, code="stale")
        assert resp.status_code == expected
        assert "invalid or expired code" not in resp.text

    def test_blank_code_is_rejected_without_calling_newapi(self, newapi_client: TestClient):
        with respx.mock(assert_all_called=False) as remote:
            route = remote.post(EXCHANGE_URL)
            resp = exchange(newapi_client, code="  ")
        assert resp.status_code == 401
        assert not route.called

    def test_unreachable_newapi_returns_502(self, newapi_client: TestClient):
        with respx.mock(assert_all_called=True) as remote:
            remote.post(EXCHANGE_URL).mock(side_effect=httpx.ConnectTimeout("timeout"))
            resp = exchange(newapi_client)
        assert resp.status_code == 502

    def test_malformed_grant_returns_502(self, newapi_client: TestClient):
        with respx.mock(assert_all_called=True) as remote:
            remote.post(EXCHANGE_URL).respond(200, json={"success": True, "data": {"user": {"id": "7"}}})
            resp = exchange(newapi_client)
        assert resp.status_code == 502

    @pytest.mark.parametrize(("allowed", "expected"), [("1, 2", 403), ("2,7", 200)])
    def test_allowed_user_ids_env_is_enforced(
        self, newapi_client: TestClient, monkeypatch: pytest.MonkeyPatch, allowed: str, expected: int
    ):
        monkeypatch.setenv("NEWAPI_ALLOWED_USER_IDS", allowed)
        with respx.mock(assert_all_called=True) as remote:
            remote.post(EXCHANGE_URL).respond(200, json=grant(user_id=7))
            resp = exchange(newapi_client)
        assert resp.status_code == expected


@pytest.mark.usefixtures("newapi_env")
class TestAutoProvider:
    async def providers(self, db_factory: async_sessionmaker[AsyncSession]):
        async with db_factory() as session:
            return await CustomProviderRepository(session).list_providers()

    async def test_first_login_saves_key_as_newapi_provider_once(
        self, newapi_client: TestClient, db_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("NEWAPI_AUTO_PROVIDER", "true")
        with respx.mock(assert_all_called=True) as remote:
            remote.post(EXCHANGE_URL).mock(
                side_effect=[httpx.Response(200, json=grant(api_key="sk-first")), httpx.Response(200, json=grant())]
            )
            assert exchange(newapi_client).status_code == 200
            assert exchange(newapi_client, code="code-2").status_code == 200

        providers = await self.providers(db_factory)
        assert [(p.discovery_format, p.base_url, p.api_key) for p in providers] == [
            ("openai", f"{NEWAPI}/v1", "sk-first")
        ]

    async def test_off_by_default(self, newapi_client: TestClient, db_factory: async_sessionmaker[AsyncSession]):
        with respx.mock(assert_all_called=True) as remote:
            remote.post(EXCHANGE_URL).respond(200, json=grant())
            assert exchange(newapi_client).status_code == 200
        assert await self.providers(db_factory) == []
