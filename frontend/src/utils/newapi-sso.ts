/**
 * new-api 统一账号登录的浏览器端两步：发起时把随机 state 与回跳地址存进 sessionStorage 后跳到
 * new-api 授权页；回到 `/sso/callback` 时取出并一次性删除，state 不一致即拒绝（防登录 CSRF）。
 * 静默登录（`prompt=none`）让已登录 new-api 的用户不经登录页直接进来；未登录时 new-api 带
 * `error=login_required` 回跳，回调页送回登录页显示表单。
 */

const STORAGE_KEY = "arcreel.newapi_sso";
const SILENT_KEY = "arcreel.newapi_sso_silent";
// 同一标签页一分钟内只静默尝试一次：避免 login_required 回到登录页后再次跳走而循环；
// 过了这个间隔（如会话失效被 401 送回登录页）会再试，new-api 仍登录时用户直接回到应用。
const SILENT_RETRY_MS = 60_000;

interface PendingLogin {
  state: string;
  from: string | null;
}

/** 跳转到 new-api 授权页；`authorizeUrl` 来自 `/auth/status`，已带 client_id 与 redirect_uri。 */
export function startNewapiLogin(authorizeUrl: string, from: string | null, silent = false): void {
  // crypto.randomUUID 只在安全上下文（HTTPS / localhost）存在，纯 HTTP 部署要用 getRandomValues。
  const state = Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
  const pending: PendingLogin = { state, from };
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(pending));
  const url = new URL(authorizeUrl);
  url.searchParams.set("state", pending.state);
  if (silent) url.searchParams.set("prompt", "none");
  window.location.assign(url.toString());
}

/** 本标签页可以静默尝试时记下本次并返回 true；最近一分钟内已尝试过则返回 false。 */
export function claimSilentLogin(now = Date.now()): boolean {
  const last = Number(sessionStorage.getItem(SILENT_KEY));
  if (last && now - last < SILENT_RETRY_MS) return false;
  sessionStorage.setItem(SILENT_KEY, String(now));
  return true;
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
