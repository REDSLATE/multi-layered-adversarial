/**
 * Brain-health regression alerting — doctrine-pinned per RedEye author
 * spec (2026-02-17):
 *
 *   1. Notify ONLY on green → degraded   OR  green → dead
 *      - NOT on degraded → dead (the brain is already known broken;
 *        a second ping is noise)
 *      - NOT on dead → degraded (improvement; not a regression)
 *      - NOT on any → green (recovery; not a regression)
 *      - NOT on first-load (no prior verdict to compare against)
 *
 *   2. Debounce ≥ DEBOUNCE_MS per brain so a flapping pod cannot
 *      machine-gun the operator.
 *
 * This module is PURE so the tripwire test can exercise it without
 * jsdom / Notification API mocks. The React side (`useEffect` watching
 * the fleet payload) calls `computeRegressions(prev, current)` and
 * fires `Notification` only for the returned brains.
 */

// 60s per brain. Matches operator's spec verbatim.
export const DEBOUNCE_MS = 60_000;

// The only transition that fires a regression alert.
const HEALTHY = "green";
const REGRESSED = new Set(["degraded", "dead"]);

/**
 * @param {Object} args
 * @param {string|undefined} args.prevVerdict      Last-seen verdict for this brain ("green"/"degraded"/"dead") or undefined on first poll.
 * @param {string} args.currentVerdict             Current verdict from the fleet payload.
 * @param {number} args.nowMs                      Date.now() at the comparison moment.
 * @param {number|undefined} args.lastNotifMs      When the last regression notification for this brain was emitted (or undefined).
 * @returns {boolean}                              True iff a notification SHOULD fire for this brain.
 */
export function shouldNotifyRegression({ prevVerdict, currentVerdict, nowMs, lastNotifMs }) {
  // First-load: no prior verdict, never alert. Otherwise the operator
  // would get pinged on every page refresh.
  if (prevVerdict === undefined || prevVerdict === null) return false;
  // Only green → {degraded, dead} qualifies as a regression.
  if (prevVerdict !== HEALTHY) return false;
  if (!REGRESSED.has(currentVerdict)) return false;
  // Debounce per brain.
  if (lastNotifMs != null && nowMs - lastNotifMs < DEBOUNCE_MS) return false;
  return true;
}

/**
 * Compute the set of brains that triggered a regression alert.
 * @param {Record<string, string>} prevVerdicts        brain -> verdict
 * @param {Record<string, string>} currentVerdicts     brain -> verdict
 * @param {Record<string, number>} lastNotifMsByBrain  brain -> epoch ms
 * @param {number} nowMs
 * @returns {string[]}  brains that should fire a notification now
 */
export function computeRegressions(prevVerdicts, currentVerdicts, lastNotifMsByBrain, nowMs) {
  const out = [];
  for (const brain of Object.keys(currentVerdicts)) {
    if (
      shouldNotifyRegression({
        prevVerdict: prevVerdicts[brain],
        currentVerdict: currentVerdicts[brain],
        nowMs,
        lastNotifMs: lastNotifMsByBrain[brain],
      })
    ) {
      out.push(brain);
    }
  }
  return out;
}
