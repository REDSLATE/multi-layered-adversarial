import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge, EmptyState } from "@/components/ui-bits";
import { Crosshair, ArrowsClockwise, Warning, Check } from "@phosphor-icons/react";

const BRAINS = ["alpha", "camaro", "chevelle", "redeye"];
const BRAIN_COLOR = {
  alpha: "#3B82F6",
  camaro: "#F59E0B",
  chevelle: "#10B981",
  redeye: "#DC2626",
};

function relTime(iso) {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export default function ExecutorSeatTile() {
  const [state, setState] = useState(null);
  const [audit, setAudit] = useState([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [confirmTarget, setConfirmTarget] = useState(null);  // 'alpha' | null | 'CLEAR'
  const [reason, setReason] = useState("");

  const load = useCallback(async () => {
    try {
      const [s, a] = await Promise.all([
        api.get("/executor"),
        api.get("/executor/audit", { params: { limit: 5 } }),
      ]);
      setState(s.data);
      setAudit(a.data?.items || []);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  const rotate = async () => {
    if (!reason.trim() || reason.trim().length < 3) {
      setErr("rotation reason must be at least 3 chars");
      return;
    }
    setBusy(true);
    setErr("");
    try {
      await api.post("/executor/rotate", {
        new_holder: confirmTarget === "CLEAR" ? null : confirmTarget,
        reason: reason.trim(),
      });
      setConfirmTarget(null);
      setReason("");
      load();
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  };

  const holder = state?.holder;
  const empty = !holder;

  return (
    <Card className="mb-6" testid="executor-seat-tile">
      <div className="flex items-start justify-between gap-3 mb-4">
        <div className="flex items-start gap-3">
          <div className="mt-0.5 text-rd-dim">
            <Crosshair size={16} weight="duotone" />
          </div>
          <div>
            <div className="font-display text-base font-bold text-rd-text leading-none">
              Executor Seat
            </div>
            <div className="text-[11px] text-rd-muted mt-1 font-mono leading-relaxed">
              Single, rotatable. Empty by default. Only the holder may route orders.
            </div>
          </div>
        </div>
        <button
          onClick={load}
          className="p-1.5 border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text"
          title="Reload"
          data-testid="exec-reload"
        >
          <ArrowsClockwise size={12} weight="bold" />
        </button>
      </div>

      {/* Current holder */}
      <div
        className={
          "border p-4 mb-4 " +
          (empty
            ? "border-rd-warn bg-rd-warn/5"
            : "border-rd-success bg-rd-success/5")
        }
        data-testid="exec-current"
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-1">
              Current Holder
            </div>
            {empty ? (
              <div className="flex items-center gap-2">
                <Warning size={14} weight="bold" className="text-rd-warn" />
                <span className="font-display text-lg font-bold text-rd-warn">
                  EMPTY
                </span>
                <span className="text-[11px] text-rd-muted font-mono">
                  no brain may execute
                </span>
              </div>
            ) : (
              <div className="flex items-center gap-2">
                <Check size={14} weight="bold" className="text-rd-success" />
                <span
                  className="font-display text-lg font-bold uppercase tracking-wide"
                  style={{ color: BRAIN_COLOR[holder] }}
                >
                  {holder}
                </span>
                <span className="text-[11px] text-rd-muted font-mono">
                  held {relTime(state?.since)}
                </span>
              </div>
            )}
          </div>
          {state?.assigned_by && (
            <div className="text-right text-[11px] font-mono text-rd-muted">
              <div>by</div>
              <div className="text-rd-text truncate max-w-[180px]">{state.assigned_by}</div>
            </div>
          )}
        </div>
        {state?.reason && state?.reason !== "empty" && (
          <div className="text-[11px] text-rd-text font-mono mt-2 pt-2 border-t border-rd-border">
            <span className="text-rd-dim">reason · </span>{state.reason}
          </div>
        )}
      </div>

      {/* Rotation controls */}
      {!confirmTarget ? (
        <div className="flex flex-wrap items-center gap-2 mb-4" data-testid="exec-rotate-controls">
          <span className="text-[10px] uppercase tracking-widest text-rd-dim mr-1">
            Rotate to
          </span>
          {BRAINS.map((b) => (
            <button
              key={b}
              disabled={b === holder}
              onClick={() => setConfirmTarget(b)}
              data-testid={`exec-rotate-${b}`}
              className={
                "px-3 py-1 text-[11px] font-mono uppercase tracking-wider border transition-colors " +
                (b === holder
                  ? "border-rd-border text-rd-dim opacity-40 cursor-not-allowed"
                  : "border-rd-border text-rd-text hover:border-rd-text hover:bg-rd-bg")
              }
              style={b !== holder ? { borderColor: BRAIN_COLOR[b] + "40" } : undefined}
            >
              {b}
            </button>
          ))}
          {!empty && (
            <button
              onClick={() => setConfirmTarget("CLEAR")}
              data-testid="exec-rotate-clear"
              className="ml-auto px-3 py-1 text-[11px] font-mono uppercase tracking-wider border border-rd-warn text-rd-warn hover:bg-rd-warn/10"
            >
              Clear seat
            </button>
          )}
        </div>
      ) : (
        <div className="border border-rd-accent bg-rd-bg p-3 mb-4" data-testid="exec-confirm-panel">
          <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2">
            Confirm rotation
          </div>
          <div className="text-[12px] font-mono text-rd-text mb-3">
            {holder ? <span className="text-rd-dim">{holder}</span> : <span className="text-rd-warn">empty</span>}
            <span className="mx-2 text-rd-dim">→</span>
            {confirmTarget === "CLEAR" ? (
              <span className="text-rd-warn">empty (cleared)</span>
            ) : (
              <span style={{ color: BRAIN_COLOR[confirmTarget] }}>{confirmTarget}</span>
            )}
          </div>
          <input
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="reason for rotation (required, ≥3 chars)"
            maxLength={1000}
            data-testid="exec-reason-input"
            className="w-full bg-rd-bg border border-rd-border px-3 py-2 font-mono text-[12px] text-rd-text focus:border-rd-accent focus:outline-none mb-2"
          />
          <div className="flex items-center justify-end gap-2">
            <button
              onClick={() => { setConfirmTarget(null); setReason(""); setErr(""); }}
              disabled={busy}
              data-testid="exec-cancel"
              className="px-3 py-1 text-[11px] font-mono uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text"
            >
              Cancel
            </button>
            <button
              onClick={rotate}
              disabled={busy || !reason.trim()}
              data-testid="exec-confirm"
              className="px-3 py-1 text-[11px] font-mono uppercase tracking-wider bg-rd-accent text-black hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {busy ? "Rotating…" : "Confirm rotation"}
            </button>
          </div>
        </div>
      )}

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-4 text-xs font-mono" data-testid="exec-error">
          {err}
        </div>
      )}

      {/* Recent rotations */}
      <div data-testid="exec-audit">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2">
          Recent rotations
        </div>
        {audit.length === 0 ? (
          <EmptyState message="No rotations yet." />
        ) : (
          <div className="divide-y divide-rd-border">
            {audit.map((r) => (
              <div key={r.rotation_id} className="py-2 flex items-start justify-between gap-3" data-testid={`audit-${r.rotation_id}`}>
                <div className="flex-1 min-w-0">
                  <div className="font-mono text-[11px] text-rd-text">
                    <span style={{ color: BRAIN_COLOR[r.previous_holder] || "#A1A1AA" }}>
                      {r.previous_holder || "empty"}
                    </span>
                    <span className="mx-1.5 text-rd-dim">→</span>
                    <span style={{ color: BRAIN_COLOR[r.new_holder] || "#A1A1AA" }}>
                      {r.new_holder || "empty"}
                    </span>
                  </div>
                  <div className="text-[10px] text-rd-muted mt-0.5 truncate">
                    {r.reason}
                  </div>
                </div>
                <div className="text-right text-[10px] font-mono text-rd-muted shrink-0">
                  <div>{relTime(r.ts)}</div>
                  <div className="text-rd-dim truncate max-w-[140px]">{r.by_admin_email}</div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </Card>
  );
}
