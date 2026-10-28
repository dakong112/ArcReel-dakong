"""new-api 统一账号登录：读取配置、交换一次性登录码、按需把密钥存为自定义供应商。

浏览器先到 new-api 的 ``/api/sso/authorize`` 取得登录码（``prompt=none`` 时不弹登录页，未登录直接带
``error=login_required`` 回跳），回到 ``NEWAPI_REDIRECT_URI`` 后由前端把
登录码交给本服务；本服务凭客户端密钥向 new-api 的 ``/api/sso/exchange`` 兑换用户身份与 API 密钥。
ArcReel 是单租户实例：任何换得身份的 new-api 用户都以实例用户身份登录，谁能进来由 new-api
上该 SSO 客户端的 ``allowed_user_ids`` 决定，``NEWAPI_ALLOWED_USER_IDS`` 是实例侧的第二道名单。

令牌里带着 new-api 登录会话 id：new-api 退出或会话被吊销后，``session_active`` 让本实例的令牌随之失效；
本实例退出时 ``logout_session`` 反过来吊销 new-api 会话（单点退出）。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from lib.db.repositories.credential_repository import CredentialRepository
from lib.db.repositories.custom_provider_repo import CustomProviderRepository

logger = logging.getLogger(__name__)

EXCHANGE_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
SESSION_TIMEOUT = httpx.Timeout(5.0, connect=3.0)
# 会话状态缓存 30 秒；new-api 暂时不可达时沿用最后一次确认的状态，最多 5 分钟，之后按失效处理。
SESSION_CACHE_TTL = 30.0
SESSION_STALE_LIMIT = 300.0
NEWAPI_PROVIDER_NAME = "new-api"
_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})

# new-api 拒绝交换时的 HTTP 状态 → (返回给浏览器的状态, 错误文案 key)；其余一律按上游故障处理。
_REJECTIONS: dict[int, tuple[int, str]] = {
    400: (401, "newapi_sso_code_invalid"),
    403: (403, "newapi_sso_denied"),
}


@dataclass(frozen=True)
class NewapiSSOConfig:
    base_url: str
    public_url: str
    client_id: str
    client_secret: str
    redirect_uri: str
    allowed_user_ids: frozenset[int]
    auto_provider: bool

    @property
    def authorize_url(self) -> str:
        """浏览器登录入口，不含 state；前端生成 state 后追加。"""
        query = urlencode({"client_id": self.client_id, "redirect_uri": self.redirect_uri})
        return f"{self.public_url}/api/sso/authorize?{query}"


@dataclass(frozen=True)
class NewapiGrant:
    user_id: int
    username: str
    api_key: str
    session_id: str


class NewapiSSOError(Exception):
    """交换失败；``message_key`` 是 ``lib.i18n`` 的错误文案 key。"""

    def __init__(self, status_code: int, message_key: str):
        super().__init__(message_key)
        self.status_code = status_code
        self.message_key = message_key


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def load_newapi_config() -> NewapiSSOConfig | None:
    """四项必填变量齐全时返回配置，否则视为未启用 new-api 登录。

    ``NEWAPI_ALLOWED_USER_IDS`` 写错时抛 ``ValueError``：名单无法解析就不放任何人进来。
    """
    base_url = _env("NEWAPI_BASE_URL").rstrip("/")
    client_id = _env("NEWAPI_CLIENT_ID")
    client_secret = _env("NEWAPI_CLIENT_SECRET")
    redirect_uri = _env("NEWAPI_REDIRECT_URI")
    if not (base_url and client_id and client_secret and redirect_uri):
        return None
    return NewapiSSOConfig(
        base_url=base_url,
        public_url=_env("NEWAPI_PUBLIC_URL").rstrip("/") or base_url,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        allowed_user_ids=frozenset(int(x) for x in _env("NEWAPI_ALLOWED_USER_IDS").replace(",", " ").split()),
        auto_provider=_env("NEWAPI_AUTO_PROVIDER").lower() in _TRUE_VALUES,
    )


async def exchange_code(config: NewapiSSOConfig, code: str) -> NewapiGrant:
    """用一次性登录码向 new-api 兑换用户身份；失败抛 ``NewapiSSOError``。

    上游的错误原文只写日志，返回给浏览器的是固定文案。
    """
    body = {
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "code": code,
        "redirect_uri": config.redirect_uri,
    }
    try:
        async with httpx.AsyncClient(timeout=EXCHANGE_TIMEOUT) as client:
            resp = await client.post(f"{config.base_url}/api/sso/exchange", json=body)
    except httpx.HTTPError as exc:
        logger.warning("new-api SSO 交换请求失败: %r", exc)
        raise NewapiSSOError(502, "newapi_sso_unreachable") from exc

    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or resp.status_code != 200 or payload.get("success") is not True:
        message = payload.get("message") if isinstance(payload, dict) else None
        logger.warning("new-api SSO 交换被拒: status=%s message=%r", resp.status_code, message)
        raise NewapiSSOError(*_REJECTIONS.get(resp.status_code, (502, "newapi_sso_failed")))

    data = payload.get("data")
    user = data.get("user") if isinstance(data, dict) else None
    user_id = user.get("id") if isinstance(user, dict) else None
    if not isinstance(data, dict) or not isinstance(user, dict) or type(user_id) is not int or user_id <= 0:
        logger.warning("new-api SSO 交换返回的用户数据无效")
        raise NewapiSSOError(502, "newapi_sso_failed")
    if config.allowed_user_ids and user_id not in config.allowed_user_ids:
        logger.warning("new-api 用户 %s 不在 NEWAPI_ALLOWED_USER_IDS 名单内，拒绝登录", user_id)
        raise NewapiSSOError(403, "newapi_sso_denied")

    # 没有会话 id 的令牌无法随 new-api 退出而失效，宁可拒绝登录。
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        logger.warning("new-api SSO 交换未返回 session_id")
        raise NewapiSSOError(502, "newapi_sso_failed")

    username = user.get("username")
    api_key = data.get("api_key")
    return NewapiGrant(
        user_id=user_id,
        username=username if isinstance(username, str) else "",
        api_key=api_key if isinstance(api_key, str) else "",
        session_id=session_id,
    )


# session_id → (是否有效, 上次检查时刻, 上次得到 new-api 答复的时刻)，均为 monotonic 秒。
_session_cache: dict[str, tuple[bool, float, float]] = {}


def remember_session(session_id: str, active: bool) -> None:
    """记下 new-api 刚确认过的会话状态（登录、退出时直接写入，免一次查询）。"""
    # ponytail: 单租户实例的会话数很少，超量时整体清空即可，不做 LRU。
    if len(_session_cache) > 1024:
        _session_cache.clear()
    now = time.monotonic()
    _session_cache[session_id] = (active, now, now)


async def _fetch_session_active(config: NewapiSSOConfig, session_id: str) -> bool:
    body = {"client_id": config.client_id, "client_secret": config.client_secret, "session_ids": [session_id]}
    async with httpx.AsyncClient(timeout=SESSION_TIMEOUT) as client:
        resp = await client.post(f"{config.base_url}/api/sso/session/status", json=body)
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data") if isinstance(payload, dict) and payload.get("success") is True else None
    active = data.get("active") if isinstance(data, dict) else None
    if not isinstance(active, dict):
        raise ValueError("new-api 会话状态响应格式无效")
    return active.get(session_id) is True


async def session_active(config: NewapiSSOConfig, session_id: str) -> bool:
    """new-api 登录会话是否仍有效；结果缓存 30 秒，查询失败时最多沿用最后答复 5 分钟。"""
    # ponytail: 缓存过期瞬间的并发请求会各查一次；单租户并发低，不做 singleflight。
    now = time.monotonic()
    entry = _session_cache.get(session_id)
    if entry and now - entry[1] < SESSION_CACHE_TTL:
        return entry[0]
    try:
        active = await _fetch_session_active(config, session_id)
    except (httpx.HTTPError, ValueError) as exc:
        if entry and now - entry[2] < SESSION_STALE_LIMIT:
            logger.warning("new-api 会话状态查询失败，沿用上次结果: %r", exc)
            _session_cache[session_id] = (entry[0], now, entry[2])
            return entry[0]
        logger.warning("new-api 会话状态查询失败且无近期结果，按失效处理: %r", exc)
        return False
    remember_session(session_id, active)
    return active


async def logout_session(config: NewapiSSOConfig, session_id: str) -> None:
    """吊销 new-api 登录会话；失败只记日志，本地退出照常进行。"""
    remember_session(session_id, False)
    body = {"client_id": config.client_id, "client_secret": config.client_secret, "session_id": session_id}
    try:
        async with httpx.AsyncClient(timeout=SESSION_TIMEOUT) as client:
            resp = await client.post(f"{config.base_url}/api/sso/session/logout", json=body)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("new-api 会话吊销失败: %r", exc)


async def ensure_newapi_provider(session: AsyncSession, config: NewapiSSOConfig, api_key: str) -> bool:
    """实例还没有任何供应商时，把登录换得的密钥存为指向 new-api 的自定义供应商。

    已有自定义供应商或预置供应商凭证时不动，运维的手工配置优先。返回是否新建。
    """
    # ponytail: 两个首次登录并发时可能各建一个供应商；单客户实例几乎不会并发首登，出现时删掉一个即可。
    if not api_key:
        return False
    providers = CustomProviderRepository(session)
    if await providers.list_providers() or await CredentialRepository(session).get_active_credentials_bulk():
        return False
    await providers.create_provider(
        display_name=NEWAPI_PROVIDER_NAME,
        discovery_format="openai",
        base_url=f"{config.base_url}/v1",
        api_key=api_key,
    )
    await session.commit()
    logger.info("已用 new-api 登录返回的密钥创建自定义供应商 %s", NEWAPI_PROVIDER_NAME)
    return True
