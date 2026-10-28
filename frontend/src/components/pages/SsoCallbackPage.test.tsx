import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { SsoCallbackPage } from "@/components/pages/SsoCallbackPage";
import { useAuthStore } from "@/stores/auth-store";

function renderCallbackAt(path: string) {
  const memory = memoryLocation({ path, record: true });
  render(
    <Router hook={memory.hook}>
      <SsoCallbackPage />
    </Router>,
  );
  return memory.history;
}

function savePending(state: string, from: string | null = null) {
  sessionStorage.setItem("arcreel.newapi_sso", JSON.stringify({ state, from }));
}

describe("SsoCallbackPage", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    sessionStorage.clear();
    useAuthStore.setState({ token: null, username: null, isAuthenticated: false, isLoading: false });
    fetchMock.mockReset().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({ access_token: "tok-sso", token_type: "bearer" }),
    } as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
  });

  it("exchanges the code and returns to the saved page when state matches", async () => {
    savePending("s-1", "/app/projects/demo");
    const history = renderCallbackAt("/sso/callback?code=c-1&state=s-1");

    await waitFor(() => expect(history.at(-1)).toBe("/app/projects/demo"));
    expect(useAuthStore.getState().token).toBe("tok-sso");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/auth/newapi/exchange");
    expect(JSON.parse(init.body as string)).toEqual({ code: "c-1" });
    expect(sessionStorage.getItem("arcreel.newapi_sso")).toBeNull();
  });

  it("rejects a state that does not match the one saved at login", async () => {
    savePending("s-1");
    renderCallbackAt("/sso/callback?code=c-1&state=forged");

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it("shows the server's message when the exchange is refused", async () => {
    savePending("s-1");
    fetchMock.mockResolvedValue({
      ok: false,
      json: vi.fn().mockResolvedValue({ detail: "denied-by-server" }),
    } as unknown as Response);
    renderCallbackAt("/sso/callback?code=c-1&state=s-1");

    expect(await screen.findByRole("alert")).toHaveTextContent("denied-by-server");
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it("returns to the login form when new-api reports login_required", async () => {
    savePending("s-1", "/app/projects/demo");
    const history = renderCallbackAt("/sso/callback?error=login_required&state=s-1");

    await waitFor(() => expect(history.at(-1)).toBe("/login?from=%2Fapp%2Fprojects%2Fdemo"));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(sessionStorage.getItem("arcreel.newapi_sso")).toBeNull();
  });
});
