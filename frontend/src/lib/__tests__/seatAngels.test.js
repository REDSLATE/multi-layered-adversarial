/**
 * Pin the seat-angel mapping (2026-02-20 marketing rebrand).
 *
 * The seat names go on:
 *   * SeatRosterStrip tiles
 *   * QuickSeatSwitches table
 *   * CouncilChamberTile header (Ignis brand)
 * If a future refactor removes the mapping, this test fails loud
 * before the rebrand silently disappears from the operator surfaces.
 *
 * Locked per operator confirmation:
 *   - Raziel  → Equity Strategist
 *   - Paschar → Equity Executor
 *   - Nuriel  → Equity Governor
 *   - Sariel  → Equity Auditor
 *   - Remiel  → Crypto Strategist
 *   - Israfel → Crypto Executor
 *   - Cassiel → Crypto Governor
 *   - Zadkiel → Crypto Auditor
 */
import {
  SEAT_ANGELS,
  seatAngel,
  seatDisplayLabel,
  IGNIS_BRAND,
} from "../seatAngels";

describe("seatAngels mapping", () => {
  test("every canonical seat key has an angel assigned", () => {
    const expectedSeats = [
      "strategist", "executor", "governor", "auditor",
      "crypto_strategist", "crypto", "crypto_governor", "crypto_auditor",
    ];
    for (const seat of expectedSeats) {
      expect(SEAT_ANGELS[seat]).toBeDefined();
      expect(SEAT_ANGELS[seat].angel).toMatch(/^[A-Z][a-z]+$/);
      expect(SEAT_ANGELS[seat].meaning.length).toBeGreaterThan(10);
    }
  });

  test("locked equity-lane assignments", () => {
    expect(seatAngel("strategist")).toBe("Raziel");
    expect(seatAngel("executor")).toBe("Paschar");
    expect(seatAngel("governor")).toBe("Nuriel");
    expect(seatAngel("auditor")).toBe("Sariel");
  });

  test("locked crypto-lane assignments", () => {
    expect(seatAngel("crypto_strategist")).toBe("Remiel");
    expect(seatAngel("crypto")).toBe("Israfel");
    expect(seatAngel("crypto_governor")).toBe("Cassiel");
    expect(seatAngel("crypto_auditor")).toBe("Zadkiel");
  });

  test("no duplicate angels across the eight seats", () => {
    const angels = Object.values(SEAT_ANGELS).map((e) => e.angel);
    const unique = new Set(angels);
    expect(unique.size).toBe(angels.length);
  });

  test("display label format: 'ANGEL · ROLE'", () => {
    expect(seatDisplayLabel("strategist")).toBe("RAZIEL · STRATEGIST");
    expect(seatDisplayLabel("crypto")).toBe("ISRAFEL · CRYPTO EXECUTOR");
    expect(seatDisplayLabel("crypto_governor")).toBe("CASSIEL · CRYPTO GOVERNOR");
  });

  test("unknown seat key falls back to bare role name", () => {
    expect(seatDisplayLabel("not_a_real_seat")).toBe("NOT A REAL SEAT");
  });

  test("null/empty seat key returns empty string", () => {
    expect(seatDisplayLabel(null)).toBe("");
    expect(seatDisplayLabel("")).toBe("");
  });

  test("Ignis brand label is exported", () => {
    expect(IGNIS_BRAND.name).toBe("Ignis");
    expect(IGNIS_BRAND.tagline.length).toBeGreaterThan(10);
  });
});
