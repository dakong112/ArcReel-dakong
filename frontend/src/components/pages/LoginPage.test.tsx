import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { LoginPage } from "@/components/pages/LoginPage";
import { useAuthStore } from "@/stores/auth-store";
import { startNewapiLogin } from "@/utils/newapi-sso";

// 跳转 new-api 会离开页面，这里只记录调用。
vi.mock("@/utils/newapi-sso", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/utils/newapi-sso")>()),
  startNewapiLogin: vi.fn(),
}));

// wouter 的 useLocation/useSearch 钩子只暴露 pathname / search，不暴露 hash，
// 无法用渲染探针断言 #fragment。改用 memoryLocation 的 record 历史：navigate
// 入参被逐字 push 进 history，因此可直接断言登录成功后导航到的完整目标
// （含 query 与 hash），从而锁住回跳链路对 hash 的保留。
function renderLoginAt(path: string) {
  const memory = memoryLocation({ path, record: true });
  const view = render(
    <Router hook={memory.hook}>
      <LoginPage />
    </Router>,
  );
  return { ...view, history: memory.history };
}

// 填表并提交。input id 来自 LoginPage（login-username / login-password），
// 用 id 选择避免依赖 i18n 解析后的 label / 按钮文案。
// 表单在 /auth/status 返回后才渲染，先等它出现。
async function submitLogin(container: HTMLElement) {
  await waitFor(() => expect(container.querySelector("form")).not.toBeNull());
  fireEvent.change(container.querySelector<HTMLInputElement>("#login-username")!, {
    target: { value: "alice" },
  });
  fireEvent.change(container.querySelector<HTMLInputElement>("#login-password")!, {
    target: { value: "pw" },
  });
  fireEvent.submit(container.querySelector("form")!);
}

describe("LoginPage returnTo consumption", () => {
  beforeEach(() => {
    useAuthStore.setState({
      token: null,
      username: null,
      isAuthenticated: false,
      isLoading: false,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: vi.fn().mockResolvedValue({ access_token: "tok-123" }),
      } as unknown as Response),
    );
  });

  // 锁住登录成功后对 ?from 的「消费」分支：读取 from → safeReturnPath 校验 → 回跳。
  // 防止以后误改回固定跳转 /app/projects 而主流程回归漏过。
  it("navigates to a valid internal ?from path after successful login", async () => {
    const { container, history } = renderLoginAt("/login?from=%2Fapp%2Fprojects%2Fdemo%3Ftab%3Dscene");
    await submitLogin(container);
    await waitFor(() => {
      expect(history.at(-1)).toBe("/app/projects/demo?tab=scene");
    });
  });

  // 锁住 hash 在回跳链路中存活：from 携带 #shot-3，登录后导航目标须保留该锚点。
  // 若源码（AuthGuard / 401 拦截 / safeReturnPath）丢掉 hash，此断言会失败。
  it("preserves the URL hash in the return path", async () => {
    const { container, history } = renderLoginAt("/login?from=%2Fapp%2Fprojects%2Fdemo%23shot-3");
    await submitLogin(container);
    await waitFor(() => {
      expect(history.at(-1)).toBe("/app/projects/demo#shot-3");
    });
  });

  it("falls back to /app/projects when ?from is an unsafe open-redirect target", async () => {
    const { container, history } = renderLoginAt("/login?from=https%3A%2F%2Fevil.com%2Fapp%2Fx");
    await submitLogin(container);
    await waitFor(() => {
      expect(history.at(-1)).toBe("/app/projects");
    });
  });

  it("falls back to /app/projects when no ?from is present", async () => {
    const { container, history } = renderLoginAt("/login");
    await submitLogin(container);
    await waitFor(() => {
      expect(history.at(-1)).toBe("/app/projects");
    });
  });
});

describe("LoginPage new-api entry", () => {
  const start = vi.mocked(startNewapiLogin);

  beforeEach(() => {
    sessionStorage.clear();
    start.mockReset();
  });

  function stubStatus(newapi: { enabled: boolean; authorize_url: string | null }) {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: vi.fn().mockResolvedValue({ enabled: true, newapi }),
      } as unknown as Response),
    );
  }

  it("silently tries new-api once per tab before showing the form", async () => {
    stubStatus({ enabled: true, authorize_url: "https://account.test/api/sso/authorize?client_id=c" });
    const first = renderLoginAt("/login?from=%2Fapp%2Fprojects%2Fdemo");
    await waitFor(() =>
      expect(start).toHaveBeenCalledWith(
        "https://account.test/api/sso/authorize?client_id=c",
        "/app/projects/demo",
        true,
      ),
    );
    expect(first.container.querySelector("form")).toBeNull();
    first.unmount();

    // login_required 回到登录页：不再跳走，显示表单。
    renderLoginAt("/login");
    expect(await screen.findByRole("button", { name: "用 new-api 登录" })).toBeInTheDocument();
    expect(start).toHaveBeenCalledTimes(1);
  });

  it("offers new-api sign-in when the server exposes an authorize URL", async () => {
    sessionStorage.setItem("arcreel.newapi_sso_silent", String(Date.now()));
    stubStatus({ enabled: true, authorize_url: "https://account.test/sso/authorize?client_id=c" });
    renderLoginAt("/login");
    expect(await screen.findByRole("button", { name: "用 new-api 登录" })).toBeInTheDocument();
  });

  it("keeps password-only login when new-api is not configured", async () => {
    stubStatus({ enabled: false, authorize_url: null });
    const { container } = renderLoginAt("/login");
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(container.querySelectorAll("button")).toHaveLength(1);
  });
});
