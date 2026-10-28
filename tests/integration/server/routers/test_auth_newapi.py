"""new-api 统一账号登录：状态接口、登录码交换、首登自动配置供应商与登录会话联动。"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import server.auth as auth_module
from lib.db import get_async_session
from lib.db.repositories.custom_provider_repo import CustomProviderRepository
from server.routers import auth as auth_router
from server.services import newapi_sso

NEWAPI = "https://newapi.test"
EXCHANGE_URL = f"{NEWAPI}/api/sso/exchange"
STATUS_URL = f"{NEWAPI}/api/sso/session/status"
LOGOUT_URL = f"{NEWAPI}/api/sso/session/logout"
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
            "session_id": "sess-1",
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
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.setattr(newapi_sso, "_session_cache", {})


@pytest.fixture
def newapi_client(db_factory: async_sessionmaker[AsyncSession]) -> Iterator[TestClient]:
    app = FastAPI()

    async def override_session():
        async with db_factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = override_session
    app.include_router(auth_router.public_router, prefix="/api/v1")
    app.include_router(auth_router.router, prefix="/api/v1", dependencies=[Depends(auth_module.get_current_user)])
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
        assert str(url.copy_with(query=None)) == "https://account.test/api/sso/authorize"
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
        assert payload["newapi_sid"] == "sess-1"
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

    def test_missing_session_id_returns_502(self, newapi_client: TestClient):
        body = grant()
        del body["data"]["session_id"]
        with respx.mock(assert_all_called=True) as remote:
            remote.post(EXCHANGE_URL).respond(200, json=body)
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


def status_body(active: bool, sid: str = "sess-1") -> dict:
    return {"success": True, "data": {"active": {sid: active}}}


def verify(client: TestClient, token: str) -> httpx.Response:
    return client.get("/api/v1/auth/verify", headers={"Authorization": f"Bearer {token}"})


def age_session(sid: str = "sess-1", *, checked: float, confirmed: float) -> None:
    """把缓存条目的检查 / 确认时刻往前拨，模拟时间流逝。"""
    active, checked_at, confirmed_at = newapi_sso._session_cache[sid]
    newapi_sso._session_cache[sid] = (active, checked_at - checked, confirmed_at - confirmed)


@pytest.mark.usefixtures("newapi_env")
class TestSessionLink:
    def login(self, client: TestClient) -> str:
        with respx.mock(assert_all_called=True) as remote:
            remote.post(EXCHANGE_URL).respond(200, json=grant())
            return exchange(client).json()["access_token"]

    def test_fresh_login_is_trusted_without_status_call(self, newapi_client: TestClient):
        token = self.login(newapi_client)
        with respx.mock(assert_all_called=False) as remote:
            route = remote.post(STATUS_URL)
            assert verify(newapi_client, token).status_code == 200
        assert not route.called

    def test_inactive_session_is_rejected_and_status_is_cached(self, newapi_client: TestClient):
        token = self.login(newapi_client)
        age_session(checked=31, confirmed=31)
        with respx.mock(assert_all_called=True) as remote:
            route = remote.post(STATUS_URL).respond(200, json=status_body(False))
            first = verify(newapi_client, token)
            second = verify(newapi_client, token)
        assert first.status_code == second.status_code == 401
        assert first.json()["detail"] == "登录已失效，请重新登录"
        assert route.call_count == 1
        assert json.loads(route.calls.last.request.content) == {
            "client_id": "arcreel-alice",
            "client_secret": "s3cret",
            "session_ids": ["sess-1"],
        }

    def test_transient_error_keeps_last_state_for_five_minutes(self, newapi_client: TestClient):
        token = self.login(newapi_client)
        age_session(checked=31, confirmed=200)
        with respx.mock(assert_all_called=True) as remote:
            remote.post(STATUS_URL).mock(side_effect=httpx.ConnectError("down"))
            assert verify(newapi_client, token).status_code == 200
            age_session(checked=31, confirmed=101)
            assert verify(newapi_client, token).status_code == 401

    def test_token_without_session_id_is_rejected(self, newapi_client: TestClient):
        assert verify(newapi_client, auth_module.create_token("newapi:7")).status_code == 401

    def test_password_login_is_not_checked(self, newapi_client: TestClient):
        with respx.mock(assert_all_called=False) as remote:
            route = remote.post(STATUS_URL)
            assert verify(newapi_client, auth_module.create_token("admin")).status_code == 200
        assert not route.called

    def test_logout_revokes_newapi_session(self, newapi_client: TestClient):
        token = self.login(newapi_client)
        with respx.mock(assert_all_called=True) as remote:
            route = remote.post(LOGOUT_URL).respond(200, json={"success": True})
            resp = newapi_client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 204
        assert json.loads(route.calls.last.request.content) == {
            "client_id": "arcreel-alice",
            "client_secret": "s3cret",
            "session_id": "sess-1",
        }
        assert verify(newapi_client, token).status_code == 401
