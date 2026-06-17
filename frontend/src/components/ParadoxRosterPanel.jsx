import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";
import { ArrowClockwise, ShieldCheck, Warning, Lightning } from "@phosphor-icons/react";

/**
 * ParadoxRosterPanel — anchored role × runtime × health matrix.
 *
 * Replaces the old eligibility-matrix editor. Under the PARADOX
 * hierarchy each role is anchored to one runtime forever — the
 * operator cannot swap them at runtime. What CAN change is the
 * health verdict for the anchored seat (e.g. executor seat goes
 * vacant when survival conditions fail).
 *
 * Auditor is intentionally absent — it is the emergent
 * paradox_record artifact, not a seat.
 *
 * 2026-02-XX — added WAKE buttons:
 *   • Per-row "WAKE" issues a signed wake-order for one brain to
 *     process a chosen ticker on its next loop.
 *   • Header "WAKE ALL" fans out to every live runtime.
 *   Wake orders do NOT bypass execution gates — they just tell
 *   the brain "look at SYMBOL". The brain still has to produce a
 *   valid intent that survives the gate chain.
 */

const ROLE_META = {
  strategist: { label: "STRATEGIST", desc: "Forms the directional reasoning. Owns the brain-side decision.",        color: "#3B82F6" },
  executor:   { label: "EXECUTOR",   desc: "Routes orders. Sole writer of broker activity.",                       color: "#F59E0B" },
  governor:   { label: "GOVERNOR",   desc: "Modulates size. May dampen, never blocks.",                            color: "#10B981" },
  opponent:   { label: "OPPONENT",   desc: "Argues the contrary case. Paradox-record co-signer.",                  color: "#DC2626" },
  memory:     { label: "MEMORY",     desc: "Learning core. Namespace-reserved (Shelly not yet running).",          color: "#A855F7" },
};

const RUNTIME_META = {
  camino:    { label: "CAMINO",    color: "#3B82F6" },
  barracuda:   { label: "BARRACUDA",   color: "#F59E0B" },
  hellcat: { label: "HELLCAT", color: "#10B981" },
  gto:   { label: "GTO",   color: "#DC2626" },
  shelly:   { label: "SHELLY",   color: "#A855F7" },
};

const LIVE_RUNTIMES = new Set(["camino", "barracuda", "hellcat", "gto"]);

const SEAT_STATUS_META = {
  occupied: { label: "OCCUPIED", color: "#22C55E" },
  vacant:   { label: "VACANT",   color: "#EF4444" },
  unknown:  { label: "UNKNOWN",  color: "#A1A1AA" },
};

function fmtSeconds(s) {
  if (s == null) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

export default function ParadoxRosterPanel() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  // Wake modal: { mode: "one" | "all", runtime?: string }
  const [wakeModal, setWakeModal] = useState(null);
  const [lastWakeByBrain, setLastWakeByBrain] = useState({});

  const refresh = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      const r = await api.get("/admin/paradox/roster");
      setData(r.data);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshRecentWakes = useCallback(async () => {
    try {
      const r = await api.get("/admin/paradox/wake-orders", { params: { hours: 6, limit: 50 } });
      const newest = {};
      for (const o of r.data?.items || []) {
        if (!newest[o.brain]) newest[o.brain] = o;
      }
      setLastWakeByBrain(newest);
    } catch (e) {
      // Non-fatal — header still works.
    }
  }, []);

  useEffect(() => {
    refresh();
    refreshRecentWakes();
  }, [refresh, refreshRecentWakes]);

  useEffect(() => {
    if (!autoRefresh) return undefined;
    const t = setInterval(() => {
      refresh();
      refreshRecentWakes();
    }, 15000);
    return () => clearInterval(t);
  }, [autoRefresh, refresh, refreshRecentWakes]);

  const onWakeSuccess = () => {
    setWakeModal(null);
    refreshRecentWakes();
  };

  return (
    <Card data-testid="paradox-roster-panel">
      <div className="flex items-center justify-between px-3 py-2 border-b border-rd-border">
        <div>
          <div className="label-eyebrow">PARADOX · roster</div>
          <div className="font-display text-base font-black tracking-tight">
            role × runtime × health
          </div>
        </div>
        <div className="flex items-center gap-2 text-xs font-mono">
          <button
            onClick={() => setWakeModal({ mode: "all" })}
            className="border border-[#F59E0B] text-[#F59E0B] px-2 py-1 hover:bg-[#F59E0B] hover:text-black flex items-center gap-1 tracking-widest font-bold"
            data-testid="paradox-roster-wake-all"
            title="Issue a wake order to every live brain"
          >
            <Lightning size={12} weight="bold" />
            WAKE ALL
          </button>
          <label className="flex items-center gap-1 text-rd-muted">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
              data-testid="paradox-roster-auto-refresh"
            />
            auto-refresh
          </label>
          <button
            onClick={refresh}
            disabled={loading}
            className="border border-rd-border px-2 py-1 text-rd-muted hover:text-rd-text disabled:opacity-40"
            data-testid="paradox-roster-refresh"
            title="Refresh"
          >
            <ArrowClockwise size={12} weight="bold" />
          </button>
        </div>
      </div>

      {err && (
        <div className="px-3 py-2 text-xs font-mono text-rd-danger border-b border-rd-border">
          {err}
        </div>
      )}

      {data && (
        <>
          <div className="px-3 py-2 text-[11px] font-mono text-rd-dim border-b border-rd-border">
            kernel: <span className="text-rd-text font-bold">{data.kernel}</span>
            <span className="mx-2">·</span>
            auditor doctrine: <span className="text-rd-text">{data.auditor_doctrine}</span>
          </div>

          <div className="divide-y divide-rd-border">
            {data.rows.map((r) => (
              <RosterRow
                key={r.role}
                row={r}
                lastWake={lastWakeByBrain[r.runtime]}
                onWake={
                  LIVE_RUNTIMES.has(r.runtime)
                    ? () => setWakeModal({ mode: "one", runtime: r.runtime })
                    : null
                }
              />
            ))}
          </div>
        </>
      )}

      {wakeModal && (
        <WakeModal
          modal={wakeModal}
          onCancel={() => setWakeModal(null)}
          onSuccess={onWakeSuccess}
        />
      )}
    </Card>
  );
}

function RosterRow({ row, lastWake, onWake }) {
  const rm = ROLE_META[row.role] || { label: row.role.toUpperCase(), desc: "", color: "#A1A1AA" };
  const rtm = RUNTIME_META[row.runtime] || { label: (row.runtime || "?").toUpperCase(), color: "#A1A1AA" };
  const ssm = SEAT_STATUS_META[row.seat_status] || SEAT_STATUS_META.unknown;
  const details = row.details || {};
  const failedReasons = details.failed_reasons || [];
  const conditions = details.conditions || {};
  const opponentMode = details.mode; // only set for opponent role

  return (
    <div
      className="px-3 py-3 space-y-2"
      data-testid={`paradox-roster-row-${row.role}`}
    >
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <span
            className="font-mono text-xs font-bold tracking-widest"
            style={{ color: rm.color }}
          >
            {rm.label}
          </span>
          <span className="text-rd-dim">→</span>
          <span
            className="font-mono text-xs font-bold tracking-widest"
            style={{ color: rtm.color }}
          >
            {rtm.label}
          </span>
        </div>
        <div className="flex items-center gap-2 text-xs font-mono">
          {row.healthy ? (
            <ShieldCheck size={12} weight="bold" style={{ color: "#22C55E" }} />
          ) : (
            <Warning size={12} weight="bold" style={{ color: "#EF4444" }} />
          )}
          <Badge color={ssm.color}>{ssm.label}</Badge>
          {opponentMode && (
            <Badge color="#06B6D4">
              {opponentMode.replace("_", " ").toUpperCase()}
            </Badge>
          )}
          {onWake && (
            <button
              onClick={onWake}
              className="border border-[#F59E0B] text-[#F59E0B] px-2 py-0.5 hover:bg-[#F59E0B] hover:text-black flex items-center gap-1 tracking-widest font-bold"
              data-testid={`paradox-roster-wake-${row.runtime}`}
              title={`Wake ${rtm.label} to process a ticker`}
            >
              <Lightning size={10} weight="bold" />
              WAKE
            </button>
          )}
        </div>
      </div>

      <div className="text-[11px] font-mono text-rd-muted">{rm.desc}</div>

      {/* Conditions readout (executor has the most; others may be empty) */}
      {Object.keys(conditions).length > 0 && (
        <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[10px] font-mono pt-1">
          {"checkin_age_s" in conditions && (
            <div className="flex justify-between border border-rd-border px-2 py-1">
              <span className="text-rd-dim uppercase tracking-widest">CHECKIN AGE</span>
              <span className={conditions.checkin_fresh ? "text-rd-text" : "text-rd-danger"}>
                {fmtSeconds(conditions.checkin_age_s)}
              </span>
            </div>
          )}
          {"checkin_hash_match" in conditions && (
            <div className="flex justify-between border border-rd-border px-2 py-1">
              <span className="text-rd-dim uppercase tracking-widest">HASH MATCH</span>
              <span className={conditions.checkin_hash_match ? "text-rd-text" : "text-rd-danger"}>
                {conditions.checkin_hash_match ? "yes" : "no"}
              </span>
            </div>
          )}
          {"recent_orphans_24h" in conditions && (
            <div className="flex justify-between border border-rd-border px-2 py-1">
              <span className="text-rd-dim uppercase tracking-widest">ORPHANS 24h</span>
              <span className={conditions.recent_orphans_24h === 0 ? "text-rd-text" : "text-rd-danger"}>
                {conditions.recent_orphans_24h}
              </span>
            </div>
          )}
          {"watchdog_armed" in conditions && (
            <div className="flex justify-between border border-rd-border px-2 py-1">
              <span className="text-rd-dim uppercase tracking-widest">WATCHDOG</span>
              <span className={conditions.watchdog_armed ? "text-rd-text" : "text-rd-danger"}>
                {conditions.watchdog_armed ? "armed" : "disarmed"}
              </span>
            </div>
          )}
        </div>
      )}

      {failedReasons.length > 0 && (
        <div
          className="text-[10px] font-mono text-rd-danger pt-1 space-y-0.5"
          data-testid={`paradox-roster-failed-${row.role}`}
        >
          {failedReasons.map((reason) => (
            <div key={reason}>✕ {reason}</div>
          ))}
        </div>
      )}

      {details.audit_implication && (
        <div className="text-[10px] font-mono text-rd-muted pt-1">
          → {details.audit_implication}
        </div>
      )}

      {lastWake && (
        <div
          className="text-[10px] font-mono pt-1 flex items-center gap-2"
          data-testid={`paradox-roster-last-wake-${row.runtime}`}
        >
          <span className="text-rd-dim uppercase tracking-widest">LAST WAKE</span>
          <span className="text-rd-text font-bold">{lastWake.ticker}</span>
          <Badge color={lastWake.status === "acked" ? "#22C55E" : (lastWake.status === "expired" ? "#A1A1AA" : "#F59E0B")}>
            {lastWake.status.toUpperCase()}
          </Badge>
          <span className="text-rd-dim">{relTimeShort(lastWake.issued_at)}</span>
        </div>
      )}
    </div>
  );
}

function relTimeShort(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

// ─────────────────────────── WakeModal ───────────────────────────────

function WakeModal({ modal, onCancel, onSuccess }) {
  const [ticker, setTicker] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [result, setResult] = useState(null);

  const isAll = modal.mode === "all";
  const targetLabel = isAll ? "ALL BRAINS" : (RUNTIME_META[modal.runtime]?.label || modal.runtime);

  const submit = async () => {
    const t = ticker.trim().toUpperCase();
    if (!t) {
      setErr("Ticker required");
      return;
    }
    setBusy(true);
    setErr("");
    try {
      const payload = { ticker: t };
      if (note.trim()) payload.note = note.trim();
      const url = isAll
        ? `/admin/paradox/wake-all`
        : `/admin/paradox/wake/${modal.runtime}`;
      const r = await api.post(url, payload);
      setResult(r.data);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  };

  // Auto-dismiss on success so the panel refresh shows the LAST WAKE
  // pill. Keep open for ~900ms so the operator sees the success state.
  useEffect(() => {
    if (!result) return undefined;
    const t = setTimeout(onSuccess, 900);
    return () => clearTimeout(t);
  }, [result, onSuccess]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
      data-testid="wake-modal"
      onClick={onCancel}
    >
      <div
        className="bg-rd-bg border border-rd-border w-full max-w-md font-mono"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-3 py-2 border-b border-rd-border text-xs text-rd-muted flex items-center gap-2">
          <Lightning size={12} weight="bold" style={{ color: "#F59E0B" }} />
          <span className="text-rd-text font-bold tracking-widest">WAKE</span>
          <span>·</span>
          <span style={{ color: "#F59E0B" }} className="font-bold tracking-widest">
            {targetLabel}
          </span>
        </div>
        <div className="p-3 space-y-3">
          <div className="text-[11px] text-rd-muted">
            Issue a signed directive to {isAll ? "every live brain" : "this brain"} to
            process the chosen ticker on its next loop. Wake orders do
            NOT bypass execution gates.
          </div>
          <div>
            <label className="text-[10px] tracking-widest text-rd-dim uppercase block mb-1">
              Ticker
            </label>
            <input
              type="text"
              value={ticker}
              onChange={(e) => setTicker(e.target.value)}
              placeholder="AAPL"
              autoFocus
              disabled={busy || !!result}
              maxLength={16}
              className="w-full bg-black border border-rd-border focus:border-rd-text focus:outline-none p-2 text-sm text-rd-text placeholder:text-rd-dim uppercase"
              data-testid="wake-modal-ticker-input"
              onKeyDown={(e) => {
                if (e.key === "Enter" && !busy && !result) submit();
              }}
            />
          </div>
          <div>
            <label className="text-[10px] tracking-widest text-rd-dim uppercase block mb-1">
              Note (optional)
            </label>
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="e.g. premarket gap, halt resumption…"
              rows={2}
              disabled={busy || !!result}
              maxLength={500}
              className="w-full bg-black border border-rd-border focus:border-rd-text focus:outline-none p-2 text-sm text-rd-text placeholder:text-rd-dim resize-y"
              data-testid="wake-modal-note-input"
            />
          </div>

          {err && (
            <div className="text-xs text-rd-danger" data-testid="wake-modal-error">
              ✕ {err}
            </div>
          )}
          {result && (
            <div className="text-xs text-rd-text border border-[#22C55E] px-2 py-1" data-testid="wake-modal-success">
              ✓ {isAll ? `${result.count} orders issued` : `order ${result.order?.order_id?.slice(0, 8)}`}
            </div>
          )}
        </div>
        <div className="px-3 py-2 border-t border-rd-border flex justify-end gap-2 text-xs">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 border border-rd-border text-rd-muted hover:text-rd-text"
            data-testid="wake-modal-cancel"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={busy || !!result || !ticker.trim()}
            className="px-3 py-1.5 border border-[#F59E0B] text-[#F59E0B] hover:bg-[#F59E0B] hover:text-black disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1 tracking-widest font-bold"
            data-testid="wake-modal-submit"
          >
            <Lightning size={10} weight="bold" />
            {busy ? "…" : (result ? "DONE" : "WAKE")}
          </button>
        </div>
      </div>
    </div>
  );
}
