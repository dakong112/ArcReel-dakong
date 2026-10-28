import { beforeEach, describe, expect, it } from "vitest";
import { claimSilentLogin } from "@/utils/newapi-sso";

describe("claimSilentLogin", () => {
  beforeEach(() => sessionStorage.clear());

  it("allows one silent attempt per tab per minute", () => {
    expect(claimSilentLogin(1_000_000)).toBe(true);
    expect(claimSilentLogin(1_000_000 + 59_999)).toBe(false);
    expect(claimSilentLogin(1_000_000 + 60_000)).toBe(true);
  });
});
