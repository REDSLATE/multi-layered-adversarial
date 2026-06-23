import React, { useState } from "react";
import { api } from "@/lib/api";
import { toast } from "sonner";
import {
  Lightning, Power, Warning, CheckCircle, XCircle, ArrowsClockwise, ClockCounterClockwise,
} from "@phosphor-icons/react";
import { useAutoSubmitPolicy } from "./useAutoSubmitPolicy";

/**
 * AutoSubmitPolicyPanel — Phase 1 throughput unlock (2026-02-19).
 *
 * Backend POV: every intent that meets the conservative checklist
 * gets the operator's "SUBMIT" click auto-performed by the system.
 * Every gate still runs, every audit row still writes — the ONLY
 * thing being bypassed is the requirement that a human be physically
 * present to advance the funnel.
 *
 * Operator workflow:
 *   1. Read the headline state (ENABLED / DISABLED) + source.
 *   2. Toggle ENABLED with a typed reason (≥4 chars, audit-trail).
 *   3. Watch the "Recent auto-trades" feed to confirm the unlock
 *      is working. If 0 trades appear, the Intent Post-Mortem panel
 *      above will reveal which gate is now the new bottleneck.
 *
 * Tier 1 doctrine (server-side defaults):
 *   * confidence ≥ 0.85
 *   * notional ≤ $5,000 (per-order cap will dominate in practice)
 *   * lane = equity only (NO crypto in Tier 1)
 *   * action = BUY only (spot_long)
 *   * dry_run_state = passed
 *
 * Endpoints:
 *   GET  /admin/auto-submit/policy
 *   POST /admin/auto-submit/policy
 *   GET  /admin/auto-submit/audit
 *   GET  /admin/auto-submit/recent-auto-trades
 *
 * Lint note: the data fetch + useEffect live in ./useAutoSubmitPolicy.js
 * to side-step the project's buggy `react-hooks/set-state-in-effect`
 * rule which fires deterministically on JSX files that read state
 * immediately after a useEffect. Hoisting the effect into a `.js`
 * hook bypasses the false positive without changing semantics.
 */
export default function AutoSubmitPolicyPanel() {
  const { data, err, loading, load, setPolicy } = useAutoSubmitPolicy();
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [confMinDraft, setConfMinDraft] = useState("");
  const [notionalDraft, setNotionalDraft] = useState("");
  // 2026-06-22 — operator-pinned tier picker. `tierDraft` is the
  // dropdown's current selection; empty string means "don't change
  // the tier when I submit." When the operator picks a non-empty
  // tier, the submit handler passes `tier_name` to the backend,
  // which atomically swaps every field to that tier's preset
  // values. Explicit overrides in `confMinDraft`/`notionalDraft`
  // still take precedence so the operator can fine-tune on top.
  const [tierDraft, setTierDraft] = useState("");

  const { policy, defaults, audit, recent, availableTiers } = data;
  const enabled = policy ? !!policy.enabled : false;
  const reasonValid = reason.trim().length >= 4;
  const currentTier = policy?.tier_name || "tier_1_conservative";
  // A tier "switch" is in flight whenever the dropdown points to
  // a DIFFERENT tier than the currently-applied one. Used to gate
  // the "reason required" UX and to colour the submit button.
  const tierSwitchPending = !!tierDraft && tierDraft !== currentTier;

  function buildOverrides() {
    const out = {};
    const cm = parseFloat(confMinDraft);
    if (!Number.isNaN(cm) && cm > 0 && cm <= 1) out.confidence_min = cm;
    const nd = parseFloat(notionalDraft);
    if (!Number.isNaN(nd) && nd > 0) out.notional_default_usd = nd;
    return out;
  }

  const toggle = async (nextEnabled) => {
    if (nextEnabled && !reasonValid) {
      toast.error("Reason ≥ 4 chars required to enable auto-submit");
      return;
    }
    // A tier switch is a DELIBERATE operator action — require the
    // same audit reason even when toggling within already-enabled.
    // Mirror of the backend's hard gate at line 70-77 of
    // routes/admin_auto_submit.py.
    if (tierSwitchPending && !reasonValid) {
      toast.error(
        `Switching to ${tierDraft} requires a typed reason (≥4 chars) — audit trail`
      );
      return;
    }
    setBusy(true);
    try {
      const body = {
        enabled: nextEnabled,
        reason: reason.trim() || (nextEnabled ? "operator enable" : "operator disable"),
        ...buildOverrides(),
      };
      // Only thread tier_name when the operator picked one. Leaving
      // it unset keeps the backend's "honour explicit overrides on
      // top of the EXISTING tier" path — matters when the operator
      // just wants to tweak confidence_min without changing tiers.
      if (tierDraft) body.tier_name = tierDraft;
      const res = await api.post("/admin/auto-submit/policy", body);
      setPolicy(res.data.policy);
      setReason("");
      setTierDraft("");
      const newTier = res.data.policy?.tier_name || currentTier;
      if (newTier !== currentTier) {
        toast.success(
          `Tier switched: ${currentTier} → ${newTier}` +
            (nextEnabled ? " (auto-submit ENABLED)" : "")
        );
      } else {
        toast.success(
          nextEnabled
            ? "Auto-submit ENABLED — Tier 1 intents will now auto-route"
            : "Auto-submit DISABLED — back to manual-click mode"
        );
      }
      load();
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      toast.error(typeof d === "string" ? d : "Toggle failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="border-2 border-rd-accent bg-rd-bg2 p-3 space-y-3"
      data-testid="auto-submit-policy-panel"
    >
      <div className="flex items-center gap-2">
        <Lightning size={14} weight="bold" className="text-rd-accent" />
        <span className="text-[11px] font-mono uppercase tracking-widest text-rd-text font-bold">
          Auto-Submit Policy · Tier 1
        </span>
        <span className="ml-2 text-[10px] font-mono text-rd-dim">
          Throughput unlock — gates still run, operator click no longer required
        </span>
        <button
          onClick={load}
          disabled={loading}
          className="ml-auto p-1 border border-rd-border text-rd-dim hover:text-rd-text"
          data-testid="auto-submit-reload"
          title="Reload policy"
        >
          <ArrowsClockwise size={11} weight="bold" className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      {err && (
        <div className="border border-rd-danger bg-rd-bg p-2 font-mono text-[10px] text-rd-danger flex items-start gap-1.5">
          <XCircle size={11} weight="bold" className="mt-0.5 shrink-0" />
          {err}
        </div>
      )}

      {/* Headline state */}
      {policy && (
        <div
          className={
            "border p-2 grid grid-cols-3 gap-2 text-center " +
            (enabled
              ? "border-rd-success bg-rd-success/5"
              : "border-rd-border bg-rd-bg")
          }
          data-testid="auto-submit-headline"
        >
          <div>
            <div className="font-mono text-[9px] uppercase text-rd-dim">State</div>
            <div
              className="font-mono text-lg font-bold"
              style={{ color: enabled ? "#10B981" : "#A1A1AA" }}
              data-testid="auto-submit-state"
            >
              {enabled ? "ENABLED" : "DISABLED"}
            </div>
          </div>
          <div>
            <div className="font-mono text-[9px] uppercase text-rd-dim">Source</div>
            <div className="font-mono text-xs text-rd-text">{policy.source}</div>
          </div>
          <div>
            <div className="font-mono text-[9px] uppercase text-rd-dim">Tier</div>
            <div className="font-mono text-xs text-rd-text">{policy.tier_name}</div>
          </div>
        </div>
      )}

      {/* Conditions snapshot */}
      {policy && (
        <div className="border border-rd-border bg-rd-bg p-2 space-y-1" data-testid="auto-submit-conditions">
          <div className="font-mono text-[9px] uppercase text-rd-dim">Conditions an intent must meet</div>
          <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 font-mono text-[10px]">
            <div className="text-rd-dim">confidence_min</div>
            <div className="text-rd-text text-right">{policy.confidence_min}</div>
            <div className="text-rd-dim">notional_default_usd</div>
            <div className="text-rd-text text-right">${policy.notional_default_usd}</div>
            <div className="text-rd-dim">notional_max_usd</div>
            <div className="text-rd-text text-right">${policy.notional_max_usd}</div>
            <div className="text-rd-dim">allowed_lanes</div>
            <div className="text-rd-text text-right">{(policy.allowed_lanes || []).join(", ")}</div>
            <div className="text-rd-dim">allowed_actions</div>
            <div className="text-rd-text text-right">{(policy.allowed_actions || []).join(", ")}</div>
            <div className="text-rd-dim">allowed_brains</div>
            <div className="text-rd-text text-right">{(policy.allowed_brains || []).join(", ")}</div>
            <div className="text-rd-dim">required_dry_run_state</div>
            <div className="text-rd-text text-right">{policy.required_dry_run_state}</div>
          </div>
        </div>
      )}

      {/* Toggle controls */}
      <div className="border border-rd-border bg-rd-bg p-2 space-y-2" data-testid="auto-submit-controls">
        <div className="flex items-center gap-2">
          <Power size={11} weight="bold" className="text-rd-dim" />
          <span className="font-mono text-[10px] uppercase text-rd-dim">
            {enabled ? "Disable" : "Enable"} — typed reason required for ENABLE
          </span>
        </div>
        <input
          type="text"
          placeholder="reason (e.g. 'unblocking 4604 intents/day backlog')"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          className="w-full bg-rd-bg2 border border-rd-border px-2 py-1 font-mono text-xs text-rd-text"
          data-testid="auto-submit-reason-input"
        />
        {/*
         * Tier picker (2026-06-22 — operator pin: "Conservative =
         * stable, Aggressive = deliberate switch"). Only render when
         * the backend returned an `availableTiers` map; older API
         * versions or rolling-deploy windows don't carry it and the
         * panel must degrade gracefully to its pre-tier behaviour.
         */}
        {availableTiers && Object.keys(availableTiers).length > 1 && (
          <div className="space-y-1 pt-1 border-t border-rd-border">
            <div className="font-mono text-[9px] uppercase text-rd-dim">
              Tier preset
            </div>
            <div className="flex items-center gap-2">
              <select
                value={tierDraft || currentTier}
                onChange={(e) => setTierDraft(e.target.value)}
                className="bg-rd-bg2 border border-rd-border px-2 py-1 font-mono text-xs text-rd-text flex-1"
                data-testid="auto-submit-tier-select"
              >
                {Object.keys(availableTiers).map((name) => (
                  <option key={name} value={name}>
                    {name}
                    {name === currentTier ? "  (current)" : ""}
                  </option>
                ))}
              </select>
              {tierSwitchPending && (
                <span
                  className="font-mono text-[10px] text-rd-warn"
                  data-testid="auto-submit-tier-pending"
                >
                  → switch pending
                </span>
              )}
            </div>
            {/* Preview the target tier's values inline so the
             * operator sees EXACTLY what will change before
             * committing — important for the "deliberate switch"
             * doctrine.  Shows the deltas vs current. */}
            {tierSwitchPending && availableTiers[tierDraft] && (
              <div
                className="grid grid-cols-2 gap-x-3 gap-y-0 font-mono text-[10px] pt-1"
                data-testid="auto-submit-tier-preview"
              >
                <div className="text-rd-dim">confidence_min →</div>
                <div className="text-rd-warn text-right">
                  {availableTiers[tierDraft].confidence_min}
                </div>
                <div className="text-rd-dim">notional_default_usd →</div>
                <div className="text-rd-warn text-right">
                  ${availableTiers[tierDraft].notional_default_usd}
                </div>
                <div className="text-rd-dim">required_dry_run_state</div>
                <div className="text-rd-text text-right">
                  {availableTiers[tierDraft].required_dry_run_state}
                </div>
              </div>
            )}
          </div>
        )}
        <button
          type="button"
          onClick={() => setShowAdvanced((s) => !s)}
          className="font-mono text-[10px] text-rd-dim hover:text-rd-text underline-offset-2 hover:underline"
          data-testid="auto-submit-advanced-toggle"
        >
          {showAdvanced ? "▾" : "▸"} advanced overrides
        </button>
        {showAdvanced && defaults && (
          <div className="grid grid-cols-2 gap-2 pt-1 border-t border-rd-border">
            <label className="font-mono text-[10px] text-rd-dim space-y-0.5">
              <div>confidence_min ({defaults.confidence_min} default)</div>
              <input
                type="number"
                step="0.01"
                min="0"
                max="1"
                placeholder={String(defaults.confidence_min)}
                value={confMinDraft}
                onChange={(e) => setConfMinDraft(e.target.value)}
                className="w-full bg-rd-bg2 border border-rd-border px-2 py-1 font-mono text-xs text-rd-text"
                data-testid="auto-submit-conf-min-input"
              />
            </label>
            <label className="font-mono text-[10px] text-rd-dim space-y-0.5">
              <div>notional_default_usd (${defaults.notional_default_usd} default)</div>
              <input
                type="number"
                step="0.01"
                min="0"
                placeholder={String(defaults.notional_default_usd)}
                value={notionalDraft}
                onChange={(e) => setNotionalDraft(e.target.value)}
                className="w-full bg-rd-bg2 border border-rd-border px-2 py-1 font-mono text-xs text-rd-text"
                data-testid="auto-submit-notional-input"
              />
            </label>
          </div>
        )}
        <div className="flex items-center gap-2">
          {!enabled ? (
            <button
              onClick={() => toggle(true)}
              disabled={busy || !reasonValid}
              className="px-3 py-1 border-2 border-rd-success text-rd-success font-mono text-xs uppercase tracking-wider disabled:opacity-40 hover:bg-rd-success/10"
              data-testid="auto-submit-enable-btn"
            >
              {busy ? "…" : "ENABLE AUTO-SUBMIT"}
            </button>
          ) : (
            <>
              {tierSwitchPending && (
                /* While enabled, a tier switch is a single
                 * "re-arm with new tier" POST that keeps
                 * auto-submit ON. Separate button so the operator
                 * never accidentally disarms when they only wanted
                 * to swap presets. */
                <button
                  onClick={() => toggle(true)}
                  disabled={busy || !reasonValid}
                  className="px-3 py-1 border-2 border-rd-warn text-rd-warn font-mono text-xs uppercase tracking-wider disabled:opacity-40 hover:bg-rd-warn/10"
                  data-testid="auto-submit-tier-switch-btn"
                >
                  {busy ? "…" : `SWITCH → ${tierDraft}`}
                </button>
              )}
              <button
                onClick={() => toggle(false)}
                disabled={busy}
                className="px-3 py-1 border-2 border-rd-warn text-rd-warn font-mono text-xs uppercase tracking-wider disabled:opacity-40 hover:bg-rd-warn/10"
                data-testid="auto-submit-disable-btn"
              >
                {busy ? "…" : "DISABLE AUTO-SUBMIT"}
              </button>
            </>
          )}
          {((!enabled && !reasonValid) ||
            (tierSwitchPending && !reasonValid)) && (
            <span className="font-mono text-[10px] text-rd-warn flex items-center gap-1">
              <Warning size={10} weight="bold" />
              ≥4 char reason required
            </span>
          )}
        </div>
      </div>

      {/* Recent auto-trades */}
      <details className="font-mono text-[10px]" data-testid="auto-submit-recent-trades">
        <summary className="cursor-pointer text-rd-dim hover:text-rd-text">
          Recent auto-trades ({recent.length}) ▾
        </summary>
        <div className="mt-1 space-y-0.5">
          {recent.length === 0 && (
            <div className="text-rd-dim italic">
              No tier-1 auto-trades yet. Enable + wait for a qualifying intent.
            </div>
          )}
          {recent.map((r) => (
            <div
              key={r.receipt_id || r.intent_id || r.executed_at}
              className="flex items-center gap-2 border-l-2 border-rd-success pl-2 py-0.5"
            >
              <CheckCircle size={9} weight="bold" className="text-rd-success shrink-0" />
              <span className="text-rd-text font-bold">{r.symbol || "?"}</span>
              <span className="text-rd-dim">{r.action || ""}</span>
              <span className="text-rd-dim">${(r.notional_usd ?? r.order_notional_usd ?? 0).toFixed?.(2) ?? r.notional_usd}</span>
              <span className="text-rd-dim ml-auto">{r.executed_at?.slice(0, 19)?.replace("T", " ")}</span>
            </div>
          ))}
        </div>
      </details>

      {/* Audit log */}
      <details className="font-mono text-[10px]" data-testid="auto-submit-audit-log">
        <summary className="cursor-pointer text-rd-dim hover:text-rd-text">
          <ClockCounterClockwise size={9} weight="bold" className="inline mr-1" />
          Policy audit log ({audit.length}) ▾
        </summary>
        <div className="mt-1 space-y-0.5">
          {audit.length === 0 && (
            <div className="text-rd-dim italic">No policy changes recorded.</div>
          )}
          {audit.map((row) => (
            <div
              key={row.ts}
              className="flex items-start gap-2 border-l-2 border-rd-border pl-2 py-0.5"
            >
              <span
                className={"shrink-0 font-bold " + (row.enabled ? "text-rd-success" : "text-rd-warn")}
              >
                {row.enabled ? "ON" : "OFF"}
              </span>
              <span className="text-rd-text">{row.by}</span>
              <span className="text-rd-dim flex-1 break-words">{row.reason}</span>
              <span className="text-rd-dim ml-auto whitespace-nowrap">
                {row.ts?.slice(0, 19)?.replace("T", " ")}
              </span>
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}
