import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { claimSilentLogin, consumeNewapiLogin, startNewapiLogin } from "@/utils/newapi-sso";

describe("claimSilentLogin", () => {
  beforeEach(() => sessionStorage.clear());

  it("allows one silent attempt per tab per minute", () => {
    expect(claimSilentLogin(1_000_000)).toBe(true);
    expect(claimSilentLogin(1_000_000 + 59_999)).toBe(false);
    expect(claimSilentLogin(1_000_000 + 60_000)).toBe(true);
  });
});

describe("startNewapiLogin", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("works without crypto.randomUUID (plain-HTTP deployments are not a secure context)", () => {
    vi.stubGlobal("crypto", { getRandomValues: crypto.getRandomValues.bind(crypto) });
    startNewapiLogin("http://newapi.test/api/sso/authorize?client_id=arcreel", "/app/projects", true);
    const { state } = JSON.parse(sessionStorage.getItem("arcreel.newapi_sso") ?? "{}") as { state?: string };
    expect(state).toMatch(/^[0-9a-f]{32}$/);
    expect(consumeNewapiLogin(state ?? null)).toEqual({ from: "/app/projects" });
  });
});
