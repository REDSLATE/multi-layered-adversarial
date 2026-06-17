import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";
import {
  Scales, ArrowsClockwise, X, Sliders, CheckCircle, XCircle,
} from "@phosphor-icons/react";
import { toast } from "sonner";

const STATE_META = {
  open:     { label: "OPEN",     color: "#3B82F6" },
  managing: { label: "MANAGING", color: "#F59E0B" },
  closed:   { label: "CLOSED",   color: "#71717A" },
};

const BRAIN_META = {
  camino:    { label: "CAMINO",    color: "#3B82F6" },
  barracuda:   { label: "BARRACUDA",   color: "#F59E0B" },
  hellcat: { label: "HELLCAT", color: "#10B981" },
  gto:   { label: "GTO",   color: "#DC2626" },
};

const FILTERS = ["open", "managing", "closed", "all"];

/**
 * Live Positions panel — operator view + management surface for the
 * trades that fired through the gate chain. Backs onto:
 *   GET    /api/admin/live-positions
 *   POST   /api/admin/live-positions/{id}/manage
 *   POST   /api/admin/live-positions/{id}/close
 *
 * Doctrine: this is observability + post-execution management. It does
 * NOT route new orders — that path stays on the gate chain. Closing
 * here broadcasts to `shared_brain_outcomes` so the scorecard pipeline
 * picks up the result.
 */
export default function LivePositionsPanel() {
  const [data, setData] = useState(null);
  const [filter, setFilter] = useState("open");
  const [busy, setBusy] = useState(false);
  const [manage, setManage] = useState(null);  // {position} or null
  const [closeFor, setCloseFor] = useState(null);
  const [guardStatus, setGuardStatus] = useState(null);  // {position_id: {fired_guard, holds, ts, …}}

  const load = useCallback(async () => {
    try {
      const params = { limit: 50 };
      if (filter !== "all") params.state = filter;
      const { data } = await api.get("/admin/live-positions", { params });
      setData(data);
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    }
  }, [filter]);

  // Roll up the most recent monitor evaluation per position so the
  // operator sees which guard last fired (or that all guards held).
  const loadGuardStatus = useCallback(async () => {
    try {
      const { data: ev } = await api.get("/admin/risk/monitor/recent-evaluations", {
        params: { limit: 200 },
      });
      const byId = {};
      for (const row of ev?.items || []) {
        // items are newest-first; keep the FIRST one we see per position.
        if (row.position_id && !byId[row.position_id]) {
          byId[row.position_id] = row;
        }
      }
      setGuardStatus(byId);
    } catch (e) {
      // Non-fatal — monitor may simply be disabled.
      console.warn("monitor evals fetch failed:", e?.message);
    }
  }, []);

  useEffect(() => { load(); loadGuardStatus(); }, [load, loadGuardStatus]);
  useEffect(() => {
    const t = setInterval(() => { load(); loadGuardStatus(); }, 15000);
    return () => clearInterval(t);
  }, [load, loadGuardStatus]);

  const totals = data?.totals || { open: 0, managing: 0, closed: 0 };

  return (
    <Card className="p-0 overflow-hidden mb-6" testid="live-positions-panel">
      <div className="px-4 py-3 border-b border-rd-border flex items-baseline justify-between gap-3 flex-wrap">
        <div className="flex items-baseline gap-3">
          <Scales size={14} weight="bold" className="text-rd-text" />
          <span className="label-eyebrow">Live positions</span>
          <Badge color="#A1A1AA">POST-EXECUTION</Badge>
        </div>
        <div className="flex items-baseline gap-3 flex-wrap">
          <span className="text-[10px] font-mono text-rd-dim">
            open <span className="text-rd-text">{totals.open}</span>
            {" · "}managing <span className="text-rd-text">{totals.managing}</span>
            {" · "}closed <span className="text-rd-text">{totals.closed}</span>
          </span>
          <button
            type="button"
            onClick={load}
            disabled={busy}
            className="text-[10px] uppercase tracking-widest text-rd-dim hover:text-rd-text flex items-center gap-1 font-mono"
            data-testid="live-positions-refresh"
          >
            <ArrowsClockwise size={10} weight="bold" /> refresh
          </button>
        </div>
      </div>

      <div className="px-4 py-2 border-b border-rd-border flex items-center gap-2 flex-wrap bg-rd-bg2">
        {FILTERS.map((f) => {
          const active = f === filter;
          return (
            <button
              key={f}
              type="button"
              onClick={() => setFilter(f)}
              className={`text-[10px] uppercase tracking-widest font-mono px-2 py-1 border ${
                active
                  ? "border-rd-text text-rd-text bg-rd-bg3"
                  : "border-rd-border text-rd-dim hover:text-rd-text"
              }`}
              data-testid={`live-positions-filter-${f}`}
            >
              {f}
            </button>
          );
        })}
      </div>

      {!data && (
        <div className="px-4 py-6 text-[11px] font-mono text-rd-dim">
          loading…
        </div>
      )}

      {data && data.items.length === 0 && (
        <div className="px-4 py-6 text-[11px] font-mono text-rd-dim italic" data-testid="live-positions-empty">
          — no positions in this state —
        </div>
      )}

      {data && data.items.length > 0 && (
        <div className="overflow-x-auto">
          <table className="text-[11px] font-mono w-full" data-testid="live-positions-table">
            <thead>
              <tr className="text-rd-dim uppercase tracking-widest text-[10px]">
                <th className="text-left py-2 px-3">state</th>
                <th className="text-left py-2 px-3">brain</th>
                <th className="text-left py-2 px-3">symbol</th>
                <th className="text-left py-2 px-3">lane</th>
                <th className="text-left py-2 px-3">side</th>
                <th className="text-right py-2 px-3">notional</th>
                <th className="text-right py-2 px-3">pnl</th>
                <th className="text-left py-2 px-3">guards</th>
                <th className="text-left py-2 px-3">opened</th>
                <th className="text-right py-2 px-3">actions</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((p) => (
                <PositionRow
                  key={p.position_id}
                  p={p}
                  busy={busy}
                  guardRow={guardStatus?.[p.position_id]}
                  onManage={() => setManage(p)}
                  onClose={() => setCloseFor(p)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="px-4 py-2.5 bg-rd-bg2 border-t border-rd-border text-[10px] text-rd-dim uppercase tracking-widest leading-relaxed">
        Close broadcasts to shared_brain_outcomes — the scorecard pipeline picks the trade up automatically.
      </div>

      {manage && (
        <ManageModal
          position={manage}
          onClose={() => setManage(null)}
          onSubmitted={async () => { setManage(null); await load(); }}
          setBusy={setBusy}
        />
      )}
      {closeFor && (
        <CloseModal
          position={closeFor}
          onClose={() => setCloseFor(null)}
          onSubmitted={async () => { setCloseFor(null); await load(); }}
          setBusy={setBusy}
        />
      )}
    </Card>
  );
}

function GuardCell({ positionId, row, isTerminal }) {
  // No monitor data yet — show a neutral "—".
  if (!row) {
    return (
      <span
        className="text-[10px] uppercase tracking-widest text-rd-dim font-mono"
        data-testid={`guard-cell-${positionId}-empty`}
      >
        {isTerminal ? "—" : "pending"}
      </span>
    );
  }
  // Skipped (unknown lane / pre-monitor)
  if (row.skipped) {
    return (
      <span
        className="text-[10px] uppercase tracking-widest text-rd-dim font-mono"
        title={row.skipped_reason || "skipped"}
        data-testid={`guard-cell-${positionId}-skipped`}
      >
        skipped
      </span>
    );
  }
  // Fatal error path
  if (row.fatal_error || row.enforce_error) {
    return (
      <span
        className="text-[10px] uppercase tracking-widest text-rd-danger font-mono"
        title={row.fatal_error || row.enforce_error}
        data-testid={`guard-cell-${positionId}-error`}
      >
        error
      </span>
    );
  }
  // A guard fired
  if (row.fired_guard) {
    const palette = {
      stop_loss: "#EF4444",
      take_profit: "#22C55E",
      trailing_stop: "#F59E0B",
      max_hold_time: "#A855F7",
    };
    const color = palette[row.fired_guard] || "#A1A1AA";
    return (
      <div
        className="flex flex-col gap-0.5"
        title={row.fired_reason || ""}
        data-testid={`guard-cell-${positionId}-fired`}
      >
        <span className="font-mono text-[10px] uppercase tracking-widest" style={{ color }}>
          <CheckCircle size={9} weight="bold" className="inline mr-1" />
          {row.fired_guard.replace(/_/g, " ")} · {row.fired_action || "—"}
        </span>
        <span className="font-mono text-[9px] text-rd-dim truncate max-w-[220px]" title={row.fired_reason || ""}>
          {row.fired_reason || ""}
        </span>
      </div>
    );
  }
  // Every guard held — show the four guard pips so the operator knows
  // the monitor evaluated this position and chose to stay flat.
  const holdsMap = {};
  for (const h of row.holds || []) holdsMap[h.guard] = h;
  const order = ["stop_loss", "take_profit", "trailing_stop", "max_hold_time"];
  return (
    <div className="flex items-center gap-1" data-testid={`guard-cell-${positionId}-holds`}>
      {order.map((g) => {
        const h = holdsMap[g];
        const action = h?.action || "—";
        const color =
          action === "HOLD" ? "#10B981" :
          action === "SKIP" ? "#71717A" :
          action === "ERROR" ? "#EF4444" :
          "#A1A1AA";
        return (
          <span
            key={g}
            className="inline-block w-1.5 h-1.5 rounded-full"
            style={{ background: color }}
            title={`${g.replace(/_/g, " ")} · ${action} · ${h?.reason || ""}`}
            data-testid={`guard-pip-${positionId}-${g}`}
          />
        );
      })}
      <span className="font-mono text-[9px] uppercase tracking-widest text-rd-dim ml-1">
        all hold
      </span>
    </div>
  );
}

function PositionRow({ p, busy, guardRow, onManage, onClose }) {
  const stateMeta = STATE_META[p.state] || { label: p.state, color: "#A1A1AA" };
  const brainMeta = BRAIN_META[p.stack] || { label: (p.stack || "?").toUpperCase(), color: "#A1A1AA" };
  const pnl = p.closed_pnl_usd;
  const pnlColor = pnl == null ? "#A1A1AA" : pnl > 0 ? "#22C55E" : pnl < 0 ? "#EF4444" : "#A1A1AA";
  const isTerminal = p.state === "closed";
  return (
    <tr className="border-t border-rd-border" data-testid={`live-position-row-${p.position_id}`}>
      <td className="py-1.5 px-3"><Badge color={stateMeta.color}>{stateMeta.label}</Badge></td>
      <td className="py-1.5 px-3"><Badge color={brainMeta.color}>{brainMeta.label}</Badge></td>
      <td className="py-1.5 px-3 text-rd-text">{p.symbol}</td>
      <td className="py-1.5 px-3 text-rd-dim uppercase">{p.lane || "—"}</td>
      <td className="py-1.5 px-3 text-rd-text">
        {p.direction === "short" ? "SHORT" : "LONG"} · {p.action}
      </td>
      <td className="py-1.5 px-3 text-right text-rd-text">
        ${(p.current_notional_usd ?? p.opened_notional_usd ?? 0).toFixed(2)}
      </td>
      <td className="py-1.5 px-3 text-right" style={{ color: pnlColor }}>
        {pnl == null ? "—" : `${pnl > 0 ? "+" : ""}$${pnl.toFixed(2)}`}
      </td>
      <td className="py-1.5 px-3">
        <GuardCell positionId={p.position_id} row={guardRow} isTerminal={isTerminal} />
      </td>
      <td className="py-1.5 px-3 text-rd-dim">{(p.opened_at || "").slice(0, 19).replace("T", " ")}</td>
      <td className="py-1.5 px-3 text-right">
        {isTerminal ? (
          <span className="text-rd-dim">
            {p.outcome_label && <Badge color={p.outcome_label === "win" ? "#22C55E" : "#EF4444"}>{p.outcome_label.toUpperCase()}</Badge>}
          </span>
        ) : (
          <div className="flex items-center gap-2 justify-end">
            <button
              type="button"
              onClick={onManage}
              disabled={busy}
              className="text-[10px] uppercase tracking-widest text-rd-dim hover:text-rd-text flex items-center gap-1 font-mono"
              data-testid={`live-position-manage-${p.position_id}`}
            >
              <Sliders size={10} weight="bold" /> manage
            </button>
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="text-[10px] uppercase tracking-widest text-rd-dim hover:text-rd-danger flex items-center gap-1 font-mono"
              data-testid={`live-position-close-${p.position_id}`}
            >
              <X size={10} weight="bold" /> close
            </button>
          </div>
        )}
      </td>
    </tr>
  );
}

function Modal({ children, onClose, testid }) {
  return (
    <div
      className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4"
      data-testid={testid}
      onClick={onClose}
    >
      <div
        className="bg-rd-bg border border-rd-border max-w-md w-full p-5 font-mono"
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}

function ManageModal({ position, onClose, onSubmitted, setBusy }) {
  const [note, setNote] = useState("");
  const [delta, setDelta] = useState("");
  const submit = async () => {
    if (!note.trim()) { toast.error("note is required"); return; }
    setBusy(true);
    try {
      const body = { note: note.trim() };
      const d = parseFloat(delta);
      if (!Number.isNaN(d)) body.delta_notional_usd = d;
      await api.post(`/admin/live-positions/${position.position_id}/manage`, body);
      toast.success("position updated");
      await onSubmitted();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal onClose={onClose} testid="live-position-manage-modal">
      <div className="label-eyebrow mb-3 flex items-center gap-2">
        <Sliders size={12} weight="bold" /> manage · {position.symbol}
      </div>
      <div className="text-[10px] text-rd-dim mb-3 leading-relaxed">
        Record an in-flight adjustment (scale, partial close, stop move). Negative delta = scale-down.
      </div>
      <label className="text-[10px] uppercase tracking-widest text-rd-dim block mb-1">note (required)</label>
      <input
        type="text"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="e.g. trimmed 30% after RSI roll-over"
        className="w-full bg-rd-bg3 border border-rd-border text-rd-text text-xs px-2 py-1.5 mb-3"
        data-testid="live-position-manage-note"
      />
      <label className="text-[10px] uppercase tracking-widest text-rd-dim block mb-1">
        delta notional (usd, optional)
      </label>
      <input
        type="number"
        step="0.01"
        value={delta}
        onChange={(e) => setDelta(e.target.value)}
        placeholder="-30.00"
        className="w-full bg-rd-bg3 border border-rd-border text-rd-text text-xs px-2 py-1.5 mb-4"
        data-testid="live-position-manage-delta"
      />
      <div className="flex items-center justify-end gap-2">
        <button type="button" onClick={onClose} className="text-[11px] uppercase tracking-widest text-rd-dim hover:text-rd-text font-mono">
          cancel
        </button>
        <button
          type="button"
          onClick={submit}
          className="text-[11px] uppercase tracking-widest text-rd-text bg-rd-bg3 border border-rd-text px-3 py-1 font-mono hover:bg-rd-bg2"
          data-testid="live-position-manage-submit"
        >
          save adjustment
        </button>
      </div>
    </Modal>
  );
}

function CloseModal({ position, onClose, onSubmitted, setBusy }) {
  const [pnl, setPnl] = useState("");
  const [pnlPct, setPnlPct] = useState("");
  const [note, setNote] = useState("");
  const [label, setLabel] = useState("");

  const autoLabel = useMemo(() => {
    const n = parseFloat(pnl);
    if (Number.isNaN(n)) return null;
    if (n > 0) return "win";
    if (n < 0) return "loss";
    return "scratch";
  }, [pnl]);

  const submit = async () => {
    setBusy(true);
    try {
      const body = { note: note.trim() };
      const n = parseFloat(pnl);
      const p = parseFloat(pnlPct);
      if (!Number.isNaN(n)) body.pnl_usd = n;
      if (!Number.isNaN(p)) body.pnl_pct = p;
      if (label) body.outcome_label = label;
      await api.post(`/admin/live-positions/${position.position_id}/close`, body);
      toast.success("position closed · broadcast to outcomes");
      await onSubmitted();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal onClose={onClose} testid="live-position-close-modal">
      <div className="label-eyebrow mb-3 flex items-center gap-2">
        <X size={12} weight="bold" /> close · {position.symbol}
      </div>
      <div className="text-[10px] text-rd-dim mb-3 leading-relaxed">
        Terminal action. Writes a shared_brain_outcomes row so the scorecards see the trade. Label auto-derives from pnl if omitted.
      </div>
      <label className="text-[10px] uppercase tracking-widest text-rd-dim block mb-1">pnl ($)</label>
      <input
        type="number"
        step="0.01"
        value={pnl}
        onChange={(e) => setPnl(e.target.value)}
        placeholder="12.50"
        className="w-full bg-rd-bg3 border border-rd-border text-rd-text text-xs px-2 py-1.5 mb-3"
        data-testid="live-position-close-pnl"
      />
      <label className="text-[10px] uppercase tracking-widest text-rd-dim block mb-1">pnl %</label>
      <input
        type="number"
        step="0.01"
        value={pnlPct}
        onChange={(e) => setPnlPct(e.target.value)}
        placeholder="2.50"
        className="w-full bg-rd-bg3 border border-rd-border text-rd-text text-xs px-2 py-1.5 mb-3"
        data-testid="live-position-close-pnl-pct"
      />
      <label className="text-[10px] uppercase tracking-widest text-rd-dim block mb-1">
        label (auto: {autoLabel || "—"})
      </label>
      <select
        value={label}
        onChange={(e) => setLabel(e.target.value)}
        className="w-full bg-rd-bg3 border border-rd-border text-rd-text text-xs px-2 py-1.5 mb-3"
        data-testid="live-position-close-label"
      >
        <option value="">(auto)</option>
        <option value="win">win</option>
        <option value="loss">loss</option>
        <option value="scratch">scratch</option>
        <option value="stopped_out">stopped_out</option>
      </select>
      <label className="text-[10px] uppercase tracking-widest text-rd-dim block mb-1">note</label>
      <input
        type="text"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="e.g. target hit"
        className="w-full bg-rd-bg3 border border-rd-border text-rd-text text-xs px-2 py-1.5 mb-4"
        data-testid="live-position-close-note"
      />
      <div className="flex items-center justify-end gap-2">
        <button type="button" onClick={onClose} className="text-[11px] uppercase tracking-widest text-rd-dim hover:text-rd-text font-mono">
          cancel
        </button>
        <button
          type="button"
          onClick={submit}
          className="text-[11px] uppercase tracking-widest text-rd-danger bg-rd-bg3 border border-rd-danger px-3 py-1 font-mono hover:bg-rd-bg2"
          data-testid="live-position-close-submit"
        >
          close position
        </button>
      </div>
    </Modal>
  );
}
