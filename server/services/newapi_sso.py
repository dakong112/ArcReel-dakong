"""new-api 统一账号登录：读取配置、交换一次性登录码、按需把密钥存为自定义供应商。

浏览器先到 new-api 的 ``/sso/authorize`` 取得登录码，回到 ``NEWAPI_REDIRECT_URI`` 后由前端把
登录码交给本服务；本服务凭客户端密钥向 new-api 的 ``/api/sso/exchange`` 兑换用户身份与 API 密钥。
ArcReel 是单租户实例：任何换得身份的 new-api 用户都以实例用户身份登录，谁能进来由 new-api
上该 SSO 客户端的 ``allowed_user_ids`` 决定，``NEWAPI_ALLOWED_USER_IDS`` 是实例侧的第二道名单。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from lib.db.repositories.credential_repository import CredentialRepository
from lib.db.repositories.custom_provider_repo import CustomProviderRepository

logger = logging.getLogger(__name__)

EXCHANGE_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
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
        return f"{self.public_url}/sso/authorize?{query}"


@dataclass(frozen=True)
class NewapiGrant:
    user_id: int
    username: str
    api_key: str


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

    username = user.get("username")
    api_key = data.get("api_key")
    return NewapiGrant(
        user_id=user_id,
        username=username if isinstance(username, str) else "",
        api_key=api_key if isinstance(api_key, str) else "",
    )


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
