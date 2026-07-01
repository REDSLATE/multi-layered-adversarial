/**
 * ParadoxV3RolloutTile — one-glance v3 rollout status (operator pin
 * 2026-02-22).
 *
 * Polls four read-only endpoints every 10s:
 *   GET /api/admin/paradox-v3/status                — flags + rollout step
 *   GET /api/admin/paradox-v3/execution-style-outcomes — per-style table
 *   GET /api/admin/doctrine/retirement-candidates    — v3 PATIENT count
 *   GET /api/admin/system-flags/changes              — flip audit feed
 *
 * (Calls drop the `/api` prefix locally — `api.js` prepends it.)
 *
 * Surfaces:
 *   • Brains: ✓/○ per brain (camino, barracuda, hellcat, gto)
 *   • Trigger watcher posture + refire posture
 *   • Patient outcomes progress: N / 50 (READY threshold)
 *   • Per-execution-style table (win rate, avg pnl, state band)
 *   • Retirement candidates flagged for v3 PATIENT scope
 *   • Overall Execution Judge state: LEARNING / READY / TRIPPED
 *
 * MUTATIONS (2026-02-23 operator pin — replaces env-var ceremony):
 *   POST /api/admin/system-flags/paradox-v3-brains
 *   POST /api/admin/system-flags/trigger-watcher
 *   POST /api/admin/system-flags/trigger-refire
 *
 * Brain circles are clickable buttons that toggle the brain on/off
 * v3. Watcher/Refire have explicit toggle controls. Every mutation
 * triggers a confirm step (refire's confirm is loud — it causes
 * live broker calls) and a fast refresh of the audit feed.
 */
import { useCallback, useEffect, useState } from "react";
import { ArrowsClockwise, CheckCircle, Circle, Warning } from "@phosphor-icons/react";
import { api } from "@/lib/api";

const POLL_MS = 10_000;
const ALL_BRAINS = ["camino", "barracuda", "hellcat", "gto"];
const READY_THRESHOLD = 50;  // pinned by operator — don't lower


function StateBadge({ state }) {
  // Conservative band styling — STRONG/HIGH_CONVICTION green, READY
  // amber, LEARNING grey, INSUFFICIENT dim.
  const styles = {
    HIGH_CONVICTION: { fg: "#10B981", label: "HIGH" },
    STRONG:          { fg: "#10B981", label: "STRONG" },
    READY:           { fg: "#F59E0B", label: "READY" },
    LEARNING:        { fg: "#6B7280", label: "LEARNING" },
    INSUFFICIENT:    { fg: "#374151", label: "—" },
  };
  const cfg = styles[state] || styles.INSUFFICIENT;
  return (
    <span
      className="font-mono text-[9px] uppercase tracking-widest px-1.5 py-0.5 border"
      style={{ color: cfg.fg, borderColor: cfg.fg + "55" }}
      data-testid={`v3-band-${state.toLowerCase()}`}
    >
      {cfg.label}
    </span>
  );
}


function ProgressBar({ value, max }) {
  const pct = Math.min(100, Math.max(0, (value / max) * 100));
  const colour = value >= max ? "#10B981" : "#F59E0B";
  return (
    <div className="relative h-1.5 bg-rd-border w-full overflow-hidden">
      <div
        className="absolute inset-y-0 left-0"
        style={{ width: `${pct}%`, backgroundColor: colour }}
      />
    </div>
  );
}


function classifyJudgeState(stylesData, retirementCount) {
  // PATIENT trade count drives the Execution Judge state surface.
  const patient = (stylesData?.styles || []).find(
    (s) => s.execution_style === "PATIENT",
  );
  const trades = patient?.trades || 0;
  if (retirementCount > 0) return "TRIPPED";
  if (trades >= READY_THRESHOLD) return "READY";
  return "LEARNING";
}


export default function ParadoxV3RolloutTile() {
  const [status, setStatus] = useState(null);
  const [styles, setStyles] = useState(null);
  const [retirement, setRetirement] = useState(null);
  const [changes, setChanges] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const [lastRefresh, setLastRefresh] = useState(null);
  // Per-flag pending state — disables the button while in flight so
  // accidental double-taps don't fire two POSTs.
  const [pending, setPending] = useState({});
  // Confirm-modal payload: { kind: "brain"|"watcher"|"refire",
  //                          label, action: () => Promise }
  const [confirm, setConfirm] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const [s, st, ret, ch] = await Promise.all([
        api.get("/admin/paradox-v3/status"),
        api.get("/admin/paradox-v3/execution-style-outcomes"),
        // Retirement candidates endpoint may not be mounted in all
        // deploys — soft-fail to null rather than break the tile.
        api.get("/admin/doctrine/retirement-candidates")
           .catch(() => ({ data: { candidates: [] } })),
        api.get("/admin/system-flags/changes?limit=5")
           .catch(() => ({ data: { changes: [] } })),
      ]);
      // api.get returns { data, status } — destructure `.data` to
      // reach the actual JSON body. Without this, every read-side
      // field on the tile (brains_on_v3, watcher/refire flags, audit
      // feed) reads as undefined and falls through to defaults.
      // Caught by testing agent iter11 / 2026-02-23.
      setStatus(s.data);
      setStyles(st.data);
      setRetirement(ret.data);
      setChanges(ch?.data?.changes || []);
      setErr(null);
      setLastRefresh(new Date());
    } catch (e) {
      setErr(e?.message || "fetch failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);

  // ── Mutations ────────────────────────────────────────────────────
  // Brain toggle: read the current list from `status.brains_on_v3`,
  // add or remove the target, POST. The backend re-validates the
  // list against ALLOWED_BRAINS so a malformed array is rejected at
  // the boundary.
  const toggleBrain = useCallback(async (brain) => {
    setPending((p) => ({ ...p, [`brain:${brain}`]: true }));
    try {
      const current = new Set((status?.brains_on_v3 || []).map((b) => b.toLowerCase()));
      const target = brain.toLowerCase();
      if (current.has(target)) current.delete(target);
      else current.add(target);
      await api.post("/admin/system-flags/paradox-v3-brains", {
        brains: Array.from(current),
      });
      await refresh();
    } catch (e) {
      setErr(e?.message || "toggle failed");
    } finally {
      setPending((p) => { const n = { ...p }; delete n[`brain:${brain}`]; return n; });
    }
  }, [status, refresh]);

  const setWatcher = useCallback(async (enabled) => {
    setPending((p) => ({ ...p, watcher: true }));
    try {
      await api.post("/admin/system-flags/trigger-watcher", { enabled });
      await refresh();
    } catch (e) {
      setErr(e?.message || "watcher toggle failed");
    } finally {
      setPending((p) => { const n = { ...p }; delete n.watcher; return n; });
    }
  }, [refresh]);

  const setRefire = useCallback(async (enabled) => {
    setPending((p) => ({ ...p, refire: true }));
    try {
      await api.post("/admin/system-flags/trigger-refire", { enabled });
      await refresh();
    } catch (e) {
      setErr(e?.message || "refire toggle failed");
    } finally {
      setPending((p) => { const n = { ...p }; delete n.refire; return n; });
    }
  }, [refresh]);

  // Modal helpers — open a confirm; on YES, run the action.
  const askBrain = (brain) => setConfirm({
    kind: "brain",
    label: `Toggle Paradox v3 emission for "${brain}"?`,
    body: "Flips this brain between v2 (legacy) and v3 envelope emission. Safe to flip in either direction — backwards compatible. Brain runner picks up the change within ~5s.",
    action: () => toggleBrain(brain),
  });
  const askWatcher = (next) => setConfirm({
    kind: "watcher",
    label: `${next ? "Enable" : "Disable"} the Trigger Watcher?`,
    body: next
      ? "Starts the watcher loop. WAIT_FOR_TRIGGER plans will be parked + monitored. Without Refire, fires are observability-only (no broker calls)."
      : "Stops the watcher loop. Any parked WAIT plans stop being monitored (existing rows are kept; the loop just goes dormant).",
    action: () => setWatcher(next),
  });
  const askRefire = (next) => setConfirm({
    kind: "refire",
    label: `${next ? "Enable" : "Disable"} live REFIRE? (real broker calls)`,
    body: next
      ? "⚠ FIRED WAIT_FOR_TRIGGER PLANS WILL TRANSLATE INTO ACTUAL BROKER ORDERS. Only enable after watching the queue drain TTL'd rows cleanly in observability mode."
      : "Stops fired plans from translating into broker calls. The watcher continues to fire/invalidate plans, but no orders are placed.",
    action: () => setRefire(next),
    danger: next,
  });

  const brainsOnV3 = new Set((status?.brains_on_v3 || []).map((b) => b.toLowerCase()));
  const patientRow = (styles?.styles || []).find(
    (s) => s.execution_style === "PATIENT",
  );
  const patientTrades = patientRow?.trades || 0;
  const v3PatientCandidates = (retirement?.candidates || []).filter(
    (c) => c.scope === "v3_patient_only",
  ).length;
  const judgeState = classifyJudgeState(styles, v3PatientCandidates);

  return (
    <div
      className="border border-rd-border bg-rd-bg p-3 space-y-3"
      data-testid="paradox-v3-rollout-tile"
    >
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-rd-dim">
            Paradox V3 Rollout
          </div>
          <div className="font-mono text-xs text-rd-text mt-0.5">
            {status?.rollout_step?.replace(/_/g, " ") || "—"}
          </div>
        </div>
        <button
          onClick={refresh}
          className="text-rd-dim hover:text-rd-text"
          data-testid="v3-tile-refresh"
          title="Refresh now"
        >
          <ArrowsClockwise size={14} />
        </button>
      </div>

      {err && (
        <div
          className="flex items-center gap-1.5 text-[10px] text-amber-500"
          data-testid="v3-tile-error"
        >
          <Warning size={12} />
          <span>{err}</span>
        </div>
      )}

      {loading && !status && (
        <div className="text-rd-dim text-[10px] py-2">Loading…</div>
      )}

      {!loading && status && (
        <>
          {/* Brains row (clickable toggles) */}
          <div data-testid="v3-tile-brains">
            <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim mb-1">
              Brains on v3 · tap to toggle
            </div>
            <div className="flex flex-wrap gap-3">
              {ALL_BRAINS.map((b) => {
                const on = brainsOnV3.has(b);
                const isPending = !!pending[`brain:${b}`];
                return (
                  <button
                    key={b}
                    type="button"
                    onClick={() => askBrain(b)}
                    disabled={isPending}
                    className={`flex items-center gap-1.5 px-2 py-1 border font-mono text-[11px] transition-colors ${
                      on
                        ? "border-rd-success/60 bg-rd-success/5"
                        : "border-rd-border hover:border-rd-text/40"
                    } ${isPending ? "opacity-50 cursor-wait" : "cursor-pointer"}`}
                    data-testid={`v3-tile-brain-${b}`}
                    aria-pressed={on}
                  >
                    {on
                      ? <CheckCircle size={12} weight="fill" color="#10B981" />
                      : <Circle size={12} color="#6B7280" />}
                    <span className={on ? "text-rd-text" : "text-rd-dim"}>
                      {b}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Patient outcomes progress */}
          <div data-testid="v3-tile-patient-progress">
            <div className="flex items-center justify-between font-mono text-[9px] uppercase tracking-widest text-rd-dim mb-1">
              <span>Patient outcomes</span>
              <span className="text-rd-text">
                {patientTrades} / {READY_THRESHOLD}
              </span>
            </div>
            <ProgressBar value={patientTrades} max={READY_THRESHOLD} />
          </div>

          {/* Per-style table */}
          {(styles?.styles || []).length > 0 && (
            <div data-testid="v3-tile-styles-table">
              <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim mb-1">
                Execution Style Outcomes
              </div>
              <table className="w-full font-mono text-[10px] border-collapse">
                <thead>
                  <tr className="text-rd-dim text-left">
                    <th className="py-1 pr-2">Style</th>
                    <th className="py-1 pr-2 text-right">Trades</th>
                    <th className="py-1 pr-2 text-right">Win%</th>
                    <th className="py-1 pr-2 text-right">Avg PnL</th>
                    <th className="py-1 text-right">State</th>
                  </tr>
                </thead>
                <tbody>
                  {styles.styles.map((row) => (
                    <tr
                      key={row.execution_style}
                      className="border-t border-rd-border/30"
                      data-testid={`v3-tile-style-row-${row.execution_style.toLowerCase()}`}
                    >
                      <td className="py-1 pr-2 text-rd-text">{row.execution_style}</td>
                      <td className="py-1 pr-2 text-right text-rd-text">{row.trades}</td>
                      <td className="py-1 pr-2 text-right text-rd-text">
                        {row.win_rate !== null ? `${(row.win_rate * 100).toFixed(0)}%` : "—"}
                      </td>
                      <td
                        className="py-1 pr-2 text-right"
                        style={{ color: row.avg_pnl_usd >= 0 ? "#10B981" : "#EF4444" }}
                      >
                        {row.avg_pnl_usd >= 0 ? "+" : ""}{row.avg_pnl_usd.toFixed(2)}
                      </td>
                      <td className="py-1 text-right">
                        <StateBadge state={row.state} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Footer summary */}
          <div className="grid grid-cols-3 gap-2 pt-1 border-t border-rd-border/30">
            <div data-testid="v3-tile-judge-state">
              <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim">
                Execution Judge
              </div>
              <div className="font-mono text-[11px] mt-0.5">
                <span
                  style={{
                    color: judgeState === "TRIPPED" ? "#EF4444" :
                           judgeState === "READY"   ? "#10B981" : "#F59E0B",
                  }}
                >
                  {judgeState}
                </span>
              </div>
            </div>
            <div data-testid="v3-tile-retirement-count">
              <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim">
                Retirement candidates
              </div>
              <div className="font-mono text-[11px] mt-0.5 text-rd-text">
                {v3PatientCandidates}
              </div>
            </div>
            <div data-testid="v3-tile-refire-state">
              <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim">
                Watcher / Refire
              </div>
              <div className="flex items-center gap-1.5 mt-0.5">
                <button
                  type="button"
                  onClick={() => askWatcher(!status.trigger_watcher_enabled)}
                  disabled={!!pending.watcher}
                  className={`font-mono text-[10px] px-1.5 py-0.5 border transition-colors ${
                    status.trigger_watcher_enabled
                      ? "border-rd-success/60 text-rd-success bg-rd-success/5"
                      : "border-rd-border text-rd-dim hover:border-rd-text/40"
                  } ${pending.watcher ? "opacity-50 cursor-wait" : "cursor-pointer"}`}
                  data-testid="v3-tile-watcher-toggle"
                  aria-pressed={!!status.trigger_watcher_enabled}
                >
                  W:{status.trigger_watcher_enabled ? "ON" : "off"}
                </button>
                <button
                  type="button"
                  onClick={() => askRefire(!status.trigger_refire_enabled)}
                  disabled={!!pending.refire}
                  className={`font-mono text-[10px] px-1.5 py-0.5 border transition-colors ${
                    status.trigger_refire_enabled
                      ? "border-rd-warn/60 text-rd-warn bg-rd-warn/5"
                      : "border-rd-border text-rd-dim hover:border-rd-text/40"
                  } ${pending.refire ? "opacity-50 cursor-wait" : "cursor-pointer"}`}
                  data-testid="v3-tile-refire-toggle"
                  aria-pressed={!!status.trigger_refire_enabled}
                  title="Live refire — fired plans translate into broker orders"
                >
                  R:{status.trigger_refire_enabled ? "ON" : "off"}
                </button>
              </div>
            </div>
          </div>

          {/* Audit feed — last 5 flag changes */}
          {changes.length > 0 && (
            <div data-testid="v3-tile-audit-feed">
              <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim mb-1">
                Recent flips
              </div>
              <ul className="space-y-0.5 font-mono text-[9px] text-rd-dim">
                {changes.slice(0, 5).map((c) => {
                  const when = c.ts
                    ? new Date(c.ts).toLocaleString(undefined, {
                        month: "numeric", day: "numeric",
                        hour: "2-digit", minute: "2-digit",
                      })
                    : "—";
                  const flag = c.flag.replace(/_/g, " ");
                  const beforeS = Array.isArray(c.before)
                    ? `[${c.before.join(",") || "∅"}]`
                    : String(c.before);
                  const afterS = Array.isArray(c.after)
                    ? `[${c.after.join(",") || "∅"}]`
                    : String(c.after);
                  const rowKey = `${c.ts || "no-ts"}-${c.flag}-${beforeS}-${afterS}`;
                  return (
                    <li
                      key={rowKey}
                      className="flex items-baseline gap-1 truncate"
                      data-testid={`v3-tile-audit-row-${c.flag}-${(c.ts || "").slice(0, 19)}`}
                    >
                      <span className="text-rd-dim shrink-0">{when}</span>
                      <span className="text-rd-text truncate">{flag}</span>
                      <span className="text-rd-dim shrink-0 ml-auto">
                        {beforeS} → {afterS}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}

          {lastRefresh && (
            <div className="font-mono text-[9px] text-rd-dim text-right">
              refreshed {lastRefresh.toLocaleTimeString()}
            </div>
          )}
        </>
      )}

      {/* Confirm modal */}
      {confirm && (
        <div
          className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center px-4"
          onClick={() => setConfirm(null)}
          data-testid="v3-tile-confirm-overlay"
        >
          <div
            className={`max-w-sm w-full border bg-rd-bg p-4 space-y-3 ${
              confirm.danger ? "border-rd-warn" : "border-rd-border"
            }`}
            onClick={(e) => e.stopPropagation()}
            data-testid="v3-tile-confirm-modal"
          >
            <div className={`font-mono text-xs uppercase tracking-widest ${
              confirm.danger ? "text-rd-warn" : "text-rd-text"
            }`}>
              {confirm.label}
            </div>
            <div className="font-mono text-[10px] text-rd-dim leading-relaxed">
              {confirm.body}
            </div>
            <div className="flex justify-end gap-2 pt-1">
              <button
                type="button"
                onClick={() => setConfirm(null)}
                className="font-mono text-[10px] uppercase tracking-widest px-3 py-1 border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text/40"
                data-testid="v3-tile-confirm-cancel"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={async () => {
                  const fn = confirm.action;
                  setConfirm(null);
                  if (fn) await fn();
                }}
                className={`font-mono text-[10px] uppercase tracking-widest px-3 py-1 border ${
                  confirm.danger
                    ? "border-rd-warn text-rd-warn hover:bg-rd-warn/10"
                    : "border-rd-success/60 text-rd-success hover:bg-rd-success/10"
                }`}
                data-testid="v3-tile-confirm-go"
              >
                {confirm.danger ? "I understand · proceed" : "Confirm"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
