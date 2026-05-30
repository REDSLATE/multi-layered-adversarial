/**
 * Brain-health regression-alert doctrine tripwires.
 *
 * Per RedEye author's spec (2026-02-17):
 *   1. Notify ONLY on green → degraded   OR  green → dead.
 *   2. NOT on the inverse (any → green = recovery, not regression).
 *   3. NOT on degraded ↔ dead flips (already broken; second ping noise).
 *   4. NOT on first-load (no prior verdict to compare against).
 *   5. Debounce ≥ 60s per brain (no flapping pod machine-gun).
 *
 * Run with:  node /app/frontend/src/lib/__tests__/brainHealthAlerts.test.mjs
 * Exits 0 on success, 1 with diagnostic line(s) on failure.
 *
 * Pure-JS — no jsdom, no jest, no DOM mocks needed because the
 * alert decision logic is intentionally pure functions.
 */
import {
  computeRegressions,
  shouldNotifyRegression,
  DEBOUNCE_MS,
} from "../brainHealthAlerts.js";

let failures = 0;
function check(label, cond, detail) {
  if (cond) {
    process.stdout.write(`  ok  ${label}\n`);
  } else {
    failures += 1;
    process.stdout.write(`  FAIL ${label}${detail ? " — " + detail : ""}\n`);
  }
}

// ── DOCTRINE PIN #1: only green → degraded/dead fires ──
check(
  "green → degraded fires",
  shouldNotifyRegression({
    prevVerdict: "green", currentVerdict: "degraded",
    nowMs: 1_000_000, lastNotifMs: undefined,
  }) === true,
);
check(
  "green → dead fires",
  shouldNotifyRegression({
    prevVerdict: "green", currentVerdict: "dead",
    nowMs: 1_000_000, lastNotifMs: undefined,
  }) === true,
);

// ── DOCTRINE PIN #2: no inverse (recovery is not a regression) ──
check(
  "degraded → green DOES NOT fire (recovery)",
  shouldNotifyRegression({
    prevVerdict: "degraded", currentVerdict: "green",
    nowMs: 1_000_000, lastNotifMs: undefined,
  }) === false,
);
check(
  "dead → green DOES NOT fire (recovery)",
  shouldNotifyRegression({
    prevVerdict: "dead", currentVerdict: "green",
    nowMs: 1_000_000, lastNotifMs: undefined,
  }) === false,
);

// ── DOCTRINE PIN #3: no degraded ↔ dead flips ──
check(
  "degraded → dead DOES NOT fire (already broken)",
  shouldNotifyRegression({
    prevVerdict: "degraded", currentVerdict: "dead",
    nowMs: 1_000_000, lastNotifMs: undefined,
  }) === false,
);
check(
  "dead → degraded DOES NOT fire (already broken, improvement)",
  shouldNotifyRegression({
    prevVerdict: "dead", currentVerdict: "degraded",
    nowMs: 1_000_000, lastNotifMs: undefined,
  }) === false,
);

// ── DOCTRINE PIN #4: first-load NEVER fires ──
check(
  "undefined → degraded DOES NOT fire (first load)",
  shouldNotifyRegression({
    prevVerdict: undefined, currentVerdict: "degraded",
    nowMs: 1_000_000, lastNotifMs: undefined,
  }) === false,
);
check(
  "undefined → dead DOES NOT fire (first load)",
  shouldNotifyRegression({
    prevVerdict: undefined, currentVerdict: "dead",
    nowMs: 1_000_000, lastNotifMs: undefined,
  }) === false,
);
check(
  "null → dead DOES NOT fire (first load)",
  shouldNotifyRegression({
    prevVerdict: null, currentVerdict: "dead",
    nowMs: 1_000_000, lastNotifMs: undefined,
  }) === false,
);

// ── DOCTRINE PIN #5: per-brain debounce ≥ 60s ──
check(
  "debounce blocks within window",
  shouldNotifyRegression({
    prevVerdict: "green", currentVerdict: "degraded",
    nowMs: 1_000_000 + 30_000, lastNotifMs: 1_000_000,
  }) === false,
  "30s after last alert should still be debounced",
);
check(
  "debounce expires after threshold",
  shouldNotifyRegression({
    prevVerdict: "green", currentVerdict: "degraded",
    nowMs: 1_000_000 + DEBOUNCE_MS + 1, lastNotifMs: 1_000_000,
  }) === true,
  `${DEBOUNCE_MS + 1}ms after last alert should fire`,
);
check(
  "DEBOUNCE_MS is exactly 60s per operator spec",
  DEBOUNCE_MS === 60_000,
  `got ${DEBOUNCE_MS}`,
);

// ── Same-verdict (no transition) cases ──
check(
  "green → green DOES NOT fire",
  shouldNotifyRegression({
    prevVerdict: "green", currentVerdict: "green",
    nowMs: 1_000_000, lastNotifMs: undefined,
  }) === false,
);
check(
  "degraded → degraded DOES NOT fire",
  shouldNotifyRegression({
    prevVerdict: "degraded", currentVerdict: "degraded",
    nowMs: 1_000_000, lastNotifMs: undefined,
  }) === false,
);
check(
  "dead → dead DOES NOT fire",
  shouldNotifyRegression({
    prevVerdict: "dead", currentVerdict: "dead",
    nowMs: 1_000_000, lastNotifMs: undefined,
  }) === false,
);

// ── computeRegressions composite behaviour ──
{
  const prev = { alpha: "green", camaro: "green", chevelle: "dead", redeye: "green" };
  const current = { alpha: "degraded", camaro: "green", chevelle: "dead", redeye: "dead" };
  const out = computeRegressions(prev, current, {}, 5_000_000);
  check(
    "computeRegressions returns only regressed brains (alpha, redeye)",
    JSON.stringify(out.sort()) === JSON.stringify(["alpha", "redeye"]),
    `got ${JSON.stringify(out)}`,
  );
}

// ── Debounce respected on composite ──
{
  const prev = { alpha: "green" };
  const current = { alpha: "degraded" };
  const lastNotif = { alpha: 4_990_000 };
  const out = computeRegressions(prev, current, lastNotif, 5_000_000);
  check(
    "computeRegressions respects per-brain debounce",
    out.length === 0,
    `expected [], got ${JSON.stringify(out)}`,
  );
}

if (failures > 0) {
  process.stderr.write(`\n${failures} doctrine tripwire(s) FAILED — alert behavior regressed.\n`);
  process.exit(1);
}
process.stdout.write("\nAll brain-health alert tripwires passed.\n");
