import { describe, expect, it } from "vitest";
import { formatCost } from "../../src/utils";

describe("formatCost", () => {
  it("formats fixed-precision decimal strings returned by the API", () => {
    expect(formatCost("0.020000000000000000", "USD")).toContain("0.02");
  });

  it("preserves the exact decimal when no currency was reported", () => {
    expect(formatCost("12345678901234567890.123456789012345678", null)).toBe(
      "12345678901234567890.123456789012345678",
    );
  });
});
