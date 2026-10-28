"""
认证 API 路由

提供 OAuth2 登录和 token 验证接口。
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from lib.db import get_async_session
from server.auth import (
    CurrentUser,
    check_credentials,
    create_token,
    is_auth_enabled,
    oauth2_scheme_optional,
    verify_token,
)
from server.i18n import Translator
from server.services.newapi_sso import (
    NewapiSSOError,
    ensure_newapi_provider,
    exchange_code,
    load_newapi_config,
    logout_session,
    remember_session,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# 公开端点：拿到 token 之前必须可达，注册时不挂 Bearer 依赖。
public_router = APIRouter()


# ==================== 响应模型 ====================


class TokenResponse(BaseModel):
    access_token: str
    token_type: str


class VerifyResponse(BaseModel):
    valid: bool
    username: str


class NewapiLoginInfo(BaseModel):
    enabled: bool
    # 浏览器登录入口（不含 state）；未启用时为 None。
    authorize_url: str | None = None


class AuthStatusResponse(BaseModel):
    enabled: bool
    newapi: NewapiLoginInfo


class NewapiExchangeRequest(BaseModel):
    # state 由前端在 sessionStorage 中校验，后端不持有登录会话，请求体里的 state 忽略。
    code: str


# ==================== 路由 ====================


@public_router.get("/auth/status", response_model=AuthStatusResponse)
async def auth_status():
    """暴露 ``AUTH_ENABLED`` 状态供前端 bootstrap 判断是否需要登录拦截。

    前端 ``auth-store.initialize()`` 在 localStorage 无 token 时调用本接口：
    ``enabled=false`` 时跳过登录页直接进主界面；``enabled=true`` 时保留原
    登录链路。本接口本身**不要求认证**——一个 boolean 比 401 探针更直观，
    且实际"是否需要登录"通过 401/200 也能从外部观察到，因此不增量泄露。
    """
    config = load_newapi_config()
    newapi = NewapiLoginInfo(enabled=config is not None, authorize_url=config.authorize_url if config else None)
    return AuthStatusResponse(enabled=is_auth_enabled(), newapi=newapi)


@public_router.post("/auth/token", response_model=TokenResponse)
async def login_for_access_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    _t: Translator,
):
    """用户登录

    使用 OAuth2 标准表单格式验证凭据，成功返回 access_token。
    ``AUTH_ENABLED=false`` 时跳过凭据校验，直接签发 token，让前端
    LoginPage 即便被打开也能正常跳转主界面。
    """
    if is_auth_enabled() and not check_credentials(form_data.username, form_data.password):
        logger.warning("登录失败: 用户名或密码错误 (用户: %s)", form_data.username)
        raise HTTPException(
            status_code=401,
            detail=_t("unauthorized"),
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_token(form_data.username)
    logger.info("用户登录成功: %s", form_data.username)
    return TokenResponse(access_token=token, token_type="bearer")


@public_router.post("/auth/newapi/exchange", response_model=TokenResponse)
async def newapi_exchange(
    body: NewapiExchangeRequest,
    request: Request,
    _t: Translator,
    session: AsyncSession = Depends(get_async_session),
):
    """用 new-api 登录码换取 ArcReel 令牌，令牌主体为 ``newapi:<new-api 用户 id>``。

    ``NEWAPI_AUTO_PROVIDER=true`` 且实例尚未配置供应商时，顺带把换得的 API 密钥存为指向
    ``NEWAPI_BASE_URL/v1`` 的自定义供应商，模型调用由 new-api 计入该用户的钱包。
    """
    config = load_newapi_config()
    if config is None:
        raise HTTPException(status_code=404, detail=_t("newapi_sso_disabled"))
    code = body.code.strip()
    if not code:
        raise HTTPException(status_code=401, detail=_t("newapi_sso_code_invalid"))
    try:
        grant = await exchange_code(config, code)
    except NewapiSSOError as exc:
        raise HTTPException(status_code=exc.status_code, detail=_t(exc.message_key)) from exc

    if config.auto_provider and await ensure_newapi_provider(session, config, grant.api_key):
        from server.services.tasks.generation_context import invalidate_backend_cache

        invalidate_backend_cache()
        worker = getattr(request.app.state, "generation_worker", None)
        if worker:
            await worker.reload_limits()

    subject = f"newapi:{grant.user_id}"
    remember_session(grant.session_id, True)
    logger.info("new-api 登录成功: %s (%s)", subject, grant.username)
    return TokenResponse(access_token=create_token(subject, newapi_session_id=grant.session_id), token_type="bearer")


@router.get("/auth/verify", response_model=VerifyResponse)
async def verify(
    current_user: CurrentUser,
):
    """验证 token 有效性

    使用 OAuth2 Bearer token 依赖自动提取和验证 token。
    """
    return VerifyResponse(valid=True, username=current_user.sub)


@router.post("/auth/logout", status_code=204)
async def logout(token: Annotated[str | None, Depends(oauth2_scheme_optional)] = None):
    """退出登录。new-api 登录的令牌顺带吊销其 new-api 会话（单点退出）；JWT 本身由前端丢弃。"""
    payload = verify_token(token) if token else None
    session_id = payload.get("newapi_sid") if payload else None
    config = load_newapi_config()
    if config and isinstance(session_id, str):
        await logout_session(config, session_id)
