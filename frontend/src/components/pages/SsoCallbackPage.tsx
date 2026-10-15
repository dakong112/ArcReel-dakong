import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Link, useLocation, useSearch } from "wouter";
import { useTranslation } from "react-i18next";
import { useAuthStore } from "@/stores/auth-store";
import { safeReturnPath } from "@/utils/safe-url";
import { consumeNewapiLogin } from "@/utils/newapi-sso";
import { errMsg } from "@/utils/async";
import type { ErrorResponse, LoginResponse } from "@/api";

/** new-api 授权后的回跳页：校验 state，用登录码换取 ArcReel 令牌后进入应用。 */
export function SsoCallbackPage() {
  const { t, i18n } = useTranslation("auth");
  const search = useSearch();
  const [, setLocation] = useLocation();
  const login = useAuthStore((s) => s.login);
  const [error, setError] = useState("");
  // 登录码只能兑换一次，StrictMode 的二次挂载不能再发请求。
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const params = new URLSearchParams(search);
    const exchange = async () => {
      const pending = consumeNewapiLogin(params.get("state"));
      if (!pending) throw new Error(t("sso_state_mismatch"));
      const resp = await fetch("/api/v1/auth/newapi/exchange", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept-Language": i18n.language || "zh" },
        body: JSON.stringify({ code: params.get("code") ?? "" }),
      });
      if (!resp.ok) {
        const data = (await resp.json().catch(() => ({}))) as Partial<ErrorResponse>;
        throw new Error(typeof data.detail === "string" ? data.detail : t("login_failed"));
      }
      const data = (await resp.json()) as LoginResponse;
      login(data.access_token, "new-api");
      setLocation(safeReturnPath(pending.from) ?? "/app/projects", { replace: true });
    };
    exchange().catch((err: unknown) => setError(errMsg(err, t("login_failed"))));
  }, [search, i18n.language, login, setLocation, t]);

  return (
    <div data-testid="sso-callback-page" className="flex h-dvh items-center justify-center bg-background p-4 text-foreground">
      {error ? (
        <div className="flex max-w-sm flex-col items-center gap-3 text-center">
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
          <Link href="/login" className="text-sm underline">
            {t("back_to_login")}
          </Link>
        </div>
      ) : (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 aria-hidden className="size-4 animate-spin" />
          {t("sso_signing_in")}
        </p>
      )}
    </div>
  );
}
