/**
 * new-api 统一账号登录的浏览器端两步：发起时把随机 state 与回跳地址存进 sessionStorage 后跳到
 * new-api 授权页；回到 `/sso/callback` 时取出并一次性删除，state 不一致即拒绝（防登录 CSRF）。
 */

const STORAGE_KEY = "arcreel.newapi_sso";

interface PendingLogin {
  state: string;
  from: string | null;
}

/** 跳转到 new-api 授权页；`authorizeUrl` 来自 `/auth/status`，已带 client_id 与 redirect_uri。 */
export function startNewapiLogin(authorizeUrl: string, from: string | null): void {
  const pending: PendingLogin = { state: crypto.randomUUID(), from };
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(pending));
  const url = new URL(authorizeUrl);
  url.searchParams.set("state", pending.state);
  window.location.assign(url.toString());
}

/** 取出并删除发起时保存的登录；`state` 不一致或没有发起记录时返回 null。 */
export function consumeNewapiLogin(state: string | null): { from: string | null } | null {
  const raw = sessionStorage.getItem(STORAGE_KEY);
  sessionStorage.removeItem(STORAGE_KEY);
  if (!raw || !state) return null;
  try {
    const pending = JSON.parse(raw) as Partial<PendingLogin>;
    return pending.state === state ? { from: pending.from ?? null } : null;
  } catch {
    return null;
  }
}
