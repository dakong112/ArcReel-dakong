from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.infra.httpx_shared import shutdown_http_client, startup_http_client
from server.auth import CurrentUserInfo, get_current_user
from server.dependencies import get_config_service
from server.i18n import get_translator
from server.routers import system_config
from tests.auth_deps import AUTH_DEPENDENCIES
from tests.factories import make_translator
from tests.http_capture import capture_http


def _make_app(version_reader: Callable[[], str] = lambda: "0.9.0") -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: CurrentUserInfo(id="u1", sub="tester")
    app.dependency_overrides[get_config_service] = lambda: MagicMock()
    app.dependency_overrides[get_translator] = lambda: make_translator()
    app.dependency_overrides[system_config.get_app_version_reader] = lambda: version_reader
    app.include_router(system_config.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    return app


@pytest.fixture(autouse=True)
def shared_http_client():
    """生产由 app lifespan 建共享客户端；这里自己建一个，respx 在 transport 层拦它。"""
    asyncio.run(startup_http_client())
    yield
    asyncio.run(shutdown_http_client())


@pytest.fixture(autouse=True)
def clear_version_cache():
    """`_read_app_version` 的 lru_cache 是模块级进程内状态。"""
    system_config._read_app_version.cache_clear()
    yield
    system_config._read_app_version.cache_clear()


class TestSystemVersionApi:
    def test_returns_current_version_without_upstream_release_check(self):
        """本 fork 不再向上游查询新版本：无出站请求，不报新版本、不带发布说明。"""
        app = _make_app()
        # capture_http 未声明任何路由：一旦出站即抛错
        with capture_http() as http, TestClient(app) as client:
            resp = client.get("/api/v1/system/version")

        assert resp.status_code == 200
        assert http.calls.call_count == 0
        body = resp.json()
        assert body["current"]["version"] == "0.9.0"
        assert body["latest"] is None
        assert body["has_update"] is False
        assert body["update_check_error"] is None
        assert datetime.fromisoformat(body["checked_at"]).tzinfo is not None

    def test_returns_500_when_local_version_cannot_be_read(self):
        def _unreadable() -> str:
            raise RuntimeError("missing version")

        app = _make_app(_unreadable)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/system/version")

        assert resp.status_code == 500
        # 同样不泄漏底层异常文本
        assert "missing version" not in str(resp.json())


class TestLoadAppVersion:
    def test_reads_project_version_from_pyproject(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "arcreel"\nversion = "1.2.3"\n', encoding="utf-8")
        assert system_config._load_app_version(pyproject) == "1.2.3"

    def test_missing_file_propagates(self, tmp_path):
        with pytest.raises(FileNotFoundError, match=r"No such file or directory"):
            system_config._load_app_version(tmp_path / "absent.toml")

    def test_empty_version_is_rejected(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "arcreel"\nversion = "  "\n', encoding="utf-8")
        with pytest.raises(RuntimeError):
            system_config._load_app_version(pyproject)
