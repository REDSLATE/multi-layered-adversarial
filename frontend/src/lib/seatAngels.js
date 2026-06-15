// Seat angel naming (2026-02-20 marketing rebrand).
//
// Maps the internal canonical seat key (strategist / executor / governor /
// auditor + crypto twins) to the public-facing angel identity used across
// operator surfaces and marketing copy.
//
// Per RISEDUAL brand architecture: the engine is "Ignis", and each seat
// inside Ignis is named for the angel whose mythology mirrors its role.
//   * Raziel  — mysteries / hidden patterns  (Equity Strategist)
//   * Paschar — vision / sees the full path  (Equity Executor)
//   * Nuriel  — fire & light / illuminates risk (Equity Governor)
//   * Sariel  — guidance / blocks dangerous decisions (Equity Auditor)
//   * Remiel  — divine visions (Crypto Strategist)
//   * Israfel — the trumpet that signals action (Crypto Executor)
//   * Cassiel — temperance / restraint (Crypto Governor)
//   * Zadkiel — mercy & judgment (Crypto Auditor)
//
// IMPORTANT: this is a DISPLAY layer only. The internal seat keys, gate
// chain, roster persistence, API surfaces, and audit log all continue
// to use strategist / executor / governor / auditor / crypto / etc.
// Nothing in /app/backend/ refers to these names. If they need to
// change for legal/trademark reasons later, the rename is a one-file
// edit here.

export const SEAT_ANGELS = {
  strategist:        { angel: "Raziel",   meaning: "Angel of mysteries · hidden-pattern signals" },
  executor:          { angel: "Paschar",  meaning: "Angel of vision · sees the full path of execution" },
  governor:          { angel: "Nuriel",   meaning: "Angel of fire & light · illuminates risk before action" },
  auditor:           { angel: "Sariel",   meaning: "Angel of guidance · blocks dangerous decisions" },
  crypto_strategist: { angel: "Remiel",   meaning: "Angel of divine visions · crypto signal generation" },
  crypto:            { angel: "Israfel",  meaning: "The trumpet · executes the moment of truth" },
  crypto_governor:   { angel: "Cassiel",  meaning: "Angel of temperance · crypto risk restraint" },
  crypto_auditor:    { angel: "Zadkiel",  meaning: "Angel of mercy & judgment · pre-trade verdict" },
};

// Friendly fallback role label (matches the canonical seat key but
// human-formatted).
const ROLE_LABELS = {
  strategist: "Strategist",
  executor: "Executor",
  governor: "Governor",
  auditor: "Auditor",
  crypto_strategist: "Crypto Strategist",
  crypto: "Crypto Executor",
  crypto_governor: "Crypto Governor",
  crypto_auditor: "Crypto Auditor",
};

/**
 * Return the marketing display string for a seat key.
 *
 * Format: `ANGEL · ROLE` (e.g. "RAZIEL · STRATEGIST") — operator-clear
 * and marketing-visible at the same time. The role keeps the operator
 * grounded; the angel carries the brand.
 *
 * Unknown / null seats fall back to the bare role name in TitleCase.
 */
export function seatDisplayLabel(seatKey) {
  if (!seatKey) return "";
  const key = String(seatKey).toLowerCase();
  const entry = SEAT_ANGELS[key];
  const role = ROLE_LABELS[key] || key.replace(/_/g, " ");
  if (!entry) return role.toUpperCase();
  return `${entry.angel.toUpperCase()} · ${role.toUpperCase()}`;
}

/**
 * Just the angel name (or null) — used where the role is already
 * visually obvious (e.g. a column header that already says "Strategist").
 */
export function seatAngel(seatKey) {
  if (!seatKey) return null;
  const entry = SEAT_ANGELS[String(seatKey).toLowerCase()];
  return entry ? entry.angel : null;
}

/**
 * Tagline / meaning string. Render in dim text under the angel name
 * for marketing tiles.
 */
export function seatAngelMeaning(seatKey) {
  if (!seatKey) return null;
  const entry = SEAT_ANGELS[String(seatKey).toLowerCase()];
  return entry ? entry.meaning : null;
}

/** Brand label for the engine container. Render on the Council Chamber tile. */
export const IGNIS_BRAND = {
  name: "Ignis",
  tagline: "Dual-core signal engine · only conviction ignites action",
};
