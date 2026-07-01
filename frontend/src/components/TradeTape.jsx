import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge, EmptyState } from "@/components/ui-bits";
import { ArrowsClockwise, Pulse, Warning, CheckCircle, XCircle, ArrowsCounterClockwise } from "@phosphor-icons/react";

const BRAIN_COLOR = {
  camino: "#3B82F6",
  barracuda: "#F59E0B",
  hellcat: "#10B981",
  gto: "#DC2626",
};

const LANE_COLOR = {
  equity: "#F59E0B",   // amber
  crypto: "#8B5CF6",   // violet
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

function fmtUsd(n) {
  if (n === null || n === undefined) return "—";
  const v = Number(n);
  if (!Number.isFinite(v)) return "—";
  return `$${v.toFixed(2)}`;
}

function verdictBadge(verdict) {
  if (!verdict) return <span className="text-rd-dim">—</span>;
  const v = String(verdict).toUpperCase();
  const color = v === "BUY" ? "#10B981" : v === "SELL" ? "#EF4444" : "#A1A1AA";
  return <Badge color={color}>{v}</Badge>;
}

/**
 * Trade Tape — the primary "what did the trader do this minute?"
 * tile. Reads from `/api/admin/trader/receipts` and `/status`,
 * both of which serve from local SQLite so this tile keeps working
 * during an Atlas outage.
 *
 * Renders:
 *   * Status strip (alive_inference / spent_today / fires_today)
 *   * Filter chips (lane, fired_only)
 *   * Dense per-cycle table: time · lane · symbol · executor ·
 *     verdict · confidence · risk verdict · broker result
 *
 * Refresh: 15s auto + manual button.
 */
export default function TradeTape() {
  const [status, setStatus] = useState(null);
  const [receipts, setReceipts] = useState([]);
  const [lane, setLane] = useState("");           // "" = both
  const [firedOnly, setFiredOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const params = { limit: 15 };
      if (lane) params.lane = lane;
      if (firedOnly) params.fired_only = true;
      const [s, r] = await Promise.all([
        api.get("/admin/trader/status"),
        api.get("/admin/trader/receipts", { params }),
      ]);
      setStatus(s.data);
      setReceipts(r.data?.items || []);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  }, [lane, firedOnly]);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  const enabled = status?.env?.TRADER_ENABLED;
  const alive = status?.loop?.alive_inference;

  return (
    <Card className="mb-6" testid="trade-tape-tile">
      {/* Header + status */}
      <div className="flex items-start justify-between gap-3 mb-4">
        <div className="flex items-start gap-3">
          <div className="mt-0.5 text-rd-dim">
            <Pulse size={16} weight="duotone" />
          </div>
          <div>
            <div className="font-display text-base font-bold text-rd-text leading-none">
              Trade Tape
            </div>
            <div className="text-[11px] text-rd-muted mt-1 font-mono leading-relaxed">
              Per-cycle log from the Sidecar Trader. Signals → seat → risk → broker, one line per lane per minute.
            </div>
          </div>
        </div>
        <button
          onClick={load}
          disabled={busy}
          className="p-1.5 border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text"
          title="Reload"
          data-testid="trade-tape-reload"
        >
          <ArrowsClockwise size={12} weight="bold" className={busy ? "animate-spin" : ""} />
        </button>
      </div>

      {/* Status strip */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-2 mb-4" data-testid="trade-tape-status-strip">
        <StatusCell
          label="Trader"
          value={
            <Badge color={enabled ? "#10B981" : "#71717A"}>
              {enabled ? "ENABLED" : "DISABLED"}
            </Badge>
          }
          testid="trade-tape-status-enabled"
        />
        <StatusCell
          label="Loop"
          value={
            <Badge color={alive ? "#10B981" : "#EF4444"}>
              {alive ? "ALIVE" : "IDLE"}
            </Badge>
          }
          testid="trade-tape-status-alive"
        />
        <StatusCell
          label="Fires today"
          value={<span className="font-mono text-sm text-rd-text">{status?.trades?.fires_today ?? "—"}</span>}
          testid="trade-tape-status-fires"
        />
        <StatusCell
          label="Spent today"
          value={<span className="font-mono text-sm text-rd-text">{fmtUsd(status?.trades?.spent_today_usd)}</span>}
          testid="trade-tape-status-spent"
        />
        <StatusCell
          label="Last cycle"
          value={<span className="font-mono text-xs text-rd-muted">{relTime(status?.loop?.last_receipt_ts)}</span>}
          testid="trade-tape-status-last"
        />
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2 mb-3" data-testid="trade-tape-filters">
        <span className="text-[10px] uppercase tracking-widest text-rd-dim mr-1">Lane</span>
        {["", "equity", "crypto"].map((l) => (
          <button
            key={l || "all"}
            onClick={() => setLane(l)}
            data-testid={`trade-tape-lane-${l || "all"}`}
            className={
              "px-2.5 py-1 text-[11px] font-mono uppercase tracking-wider border transition-colors " +
              (lane === l
                ? "border-rd-text text-rd-text"
                : "border-rd-border text-rd-dim hover:text-rd-text")
            }
            style={l && lane === l ? { borderColor: LANE_COLOR[l], color: LANE_COLOR[l] } : undefined}
          >
            {l || "both"}
          </button>
        ))}
        <label className="flex items-center gap-1.5 ml-3 cursor-pointer" data-testid="trade-tape-fired-toggle-label">
          <input
            type="checkbox"
            checked={firedOnly}
            onChange={(e) => setFiredOnly(e.target.checked)}
            data-testid="trade-tape-fired-toggle"
            className="accent-rd-accent"
          />
          <span className="text-[11px] font-mono text-rd-muted uppercase tracking-wider">
            fired only
          </span>
        </label>
      </div>

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-3 text-xs font-mono" data-testid="trade-tape-error">
          {err}
        </div>
      )}

      {/* Tape rows */}
      {receipts.length === 0 ? (
        <EmptyState
          message={
            enabled
              ? "No cycles recorded yet. Trader is enabled — first receipt appears within one interval."
              : "Trader is disabled. Set TRADER_ENABLED=true to activate the loop."
          }
          testid="trade-tape-empty"
        />
      ) : (
        <div className="border border-rd-border divide-y divide-rd-border" data-testid="trade-tape-list">
          {/* Column header */}
          <div className="grid grid-cols-12 gap-2 px-2 py-1.5 bg-rd-bg text-[9px] uppercase tracking-widest text-rd-dim font-mono">
            <div className="col-span-2">time · lane</div>
            <div className="col-span-1">sym</div>
            <div className="col-span-2">executor</div>
            <div className="col-span-1">verdict</div>
            <div className="col-span-1">conf</div>
            <div className="col-span-2">risk</div>
            <div className="col-span-3">broker · error</div>
          </div>
          {receipts.map((r) => (
            <TapeRow key={`${r.cycle_id}-${r.lane}-${r.ts}`} r={r} />
          ))}
        </div>
      )}
    </Card>
  );
}

function StatusCell({ label, value, testid }) {
  return (
    <div className="border border-rd-border bg-rd-bg px-2 py-1.5" data-testid={testid}>
      <div className="text-[9px] uppercase tracking-widest text-rd-dim">{label}</div>
      <div className="mt-0.5">{value}</div>
    </div>
  );
}

function TapeRow({ r }) {
  const chosen = r.chosen || {};
  const seats = r.seats || {};
  const executor = seats.executor || chosen.brain;
  const verdict = chosen.verdict;
  const conf = chosen.confidence;
  const risk = r.risk || {};
  const fired = r.broker_result && !r.error;
  const laneColor = LANE_COLOR[r.lane] || "#A1A1AA";
  const rowKey = `${r.cycle_id || "no-cyc"}-${r.lane || "no-lane"}`;

  return (
    <div
      className="grid grid-cols-12 gap-2 px-2 py-1.5 items-center hover:bg-rd-bg/50 text-[11px] font-mono"
      data-testid={`trade-tape-row-${rowKey}`}
    >
      <div className="col-span-2 min-w-0">
        <div className="text-rd-muted text-[10px]">{relTime(r.ts)}</div>
        <div className="uppercase tracking-widest text-[9px]" style={{ color: laneColor }}>
          {r.lane || "—"}
        </div>
      </div>
      <div className="col-span-1 text-rd-text truncate">{r.symbol || "—"}</div>
      <div className="col-span-2 min-w-0">
        {executor ? (
          <span
            className="uppercase tracking-wide font-bold"
            style={{ color: BRAIN_COLOR[executor] || "#A1A1AA" }}
          >
            {executor}
          </span>
        ) : (
          <span className="text-rd-dim">vacant</span>
        )}
      </div>
      <div className="col-span-1">{verdictBadge(verdict)}</div>
      <div className="col-span-1 text-rd-text">
        {typeof conf === "number" ? conf.toFixed(2) : "—"}
      </div>
      <div className="col-span-2 min-w-0 truncate" title={risk.reason || ""}>
        {risk.reason ? (
          <span className={risk.ok === false ? "text-rd-warn" : "text-rd-muted"}>
            {risk.reason}
          </span>
        ) : (
          <span className="text-rd-dim">—</span>
        )}
      </div>
      <div className="col-span-3 min-w-0 flex items-center gap-1.5">
        {r.error ? (
          <>
            <XCircle size={12} weight="bold" className="text-rd-danger shrink-0" />
            <span className="text-rd-danger truncate" title={r.error}>{r.error}</span>
          </>
        ) : fired ? (
          <>
            <CheckCircle size={12} weight="bold" className="text-rd-success shrink-0" />
            <span className="text-rd-success">FIRED</span>
            {r.broker_result?.order_id && (
              <span className="text-rd-muted text-[10px] truncate">
                · {String(r.broker_result.order_id).slice(0, 12)}
              </span>
            )}
          </>
        ) : verdict === "HOLD" ? (
          <>
            <ArrowsCounterClockwise size={12} weight="bold" className="text-rd-dim shrink-0" />
            <span className="text-rd-dim">hold</span>
          </>
        ) : (
          <>
            <Warning size={12} weight="bold" className="text-rd-dim shrink-0" />
            <span className="text-rd-dim">pass</span>
          </>
        )}
      </div>
    </div>
  );
}
