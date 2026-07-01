import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge, EmptyState } from "@/components/ui-bits";
import { ArrowsClockwise, WaveTriangle } from "@phosphor-icons/react";

const SOURCE_COLOR = { kraken: "#8B5CF6", webull: "#F59E0B" };
const LANE_COLOR = { equity: "#F59E0B", crypto: "#8B5CF6" };

function relTime(iso) {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

function fmtBps(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return "—";
  return `${Number(v).toFixed(2)} bps`;
}

function fmtPrice(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return "—";
  const n = Number(v);
  if (n >= 1000) return n.toFixed(2);
  if (n >= 1) return n.toFixed(3);
  return n.toFixed(5);
}

/**
 * Spread Watcher — live bid/ask spread for the trader's tracked
 * symbols. Reads `/api/admin/trader/spread` which serves from the
 * local SQLite + in-memory cache (no Mongo dependency, works during
 * Atlas outages).
 *
 * Two independent pollers back this: Kraken /public/Ticker for the
 * crypto pair(s) and Webull's public quote gateway for the equity
 * ticker(s). Both are gated observability-only unless the operator
 * flips TRADER_SPREAD_GATE_ENABLED / TRADER_EQUITY_SPREAD_GATE_ENABLED.
 */
export default function SpreadWatcher() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const r = await api.get("/admin/trader/spread", { params: { limit: 120 } });
      setData(r.data);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  const latest = useMemo(
    () => (Array.isArray(data?.latest) ? data.latest : []),
    [data],
  );
  const cfg = data?.config || {};

  return (
    <Card className="mb-6" testid="spread-watcher-tile">
      <div className="flex items-start justify-between gap-3 mb-4">
        <div className="flex items-start gap-3">
          <div className="mt-0.5 text-rd-dim">
            <WaveTriangle size={16} weight="duotone" />
          </div>
          <div>
            <div className="font-display text-base font-bold text-rd-text leading-none">
              Spread Watcher
            </div>
            <div className="text-[11px] text-rd-muted mt-1 font-mono leading-relaxed">
              Live bid/ask spread. Kraken (crypto) + Webull public gateway (equity). Optional risk gate.
            </div>
          </div>
        </div>
        <button
          onClick={load}
          disabled={busy}
          className="p-1.5 border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text"
          title="Reload"
          data-testid="spread-watcher-reload"
        >
          <ArrowsClockwise size={12} weight="bold" className={busy ? "animate-spin" : ""} />
        </button>
      </div>

      {err && (
        <div
          className="border border-rd-danger text-rd-danger px-3 py-2 mb-3 text-xs font-mono"
          data-testid="spread-watcher-error"
        >
          {err}
        </div>
      )}

      {/* Config chips */}
      <div className="flex flex-wrap items-center gap-2 mb-4" data-testid="spread-watcher-config">
        <GateChip
          label="crypto gate"
          on={cfg?.crypto?.gate_enabled}
          bps={cfg?.crypto?.max_bps}
          testid="spread-cfg-crypto"
        />
        <GateChip
          label="equity gate"
          on={cfg?.equity?.gate_enabled}
          bps={cfg?.equity?.max_bps}
          testid="spread-cfg-equity"
        />
        <span className="text-[10px] font-mono text-rd-dim uppercase tracking-widest">
          stale after {cfg?.stale_sec ?? "—"}s
        </span>
      </div>

      {/* Latest snapshot per symbol */}
      {latest.length === 0 ? (
        <EmptyState
          message="Spread poller hasn't produced a reading yet. First tick appears within one interval."
          testid="spread-watcher-empty"
        />
      ) : (
        <div className="border border-rd-border divide-y divide-rd-border" data-testid="spread-watcher-latest">
          <div className="grid grid-cols-12 gap-2 px-2 py-1.5 bg-rd-bg text-[9px] uppercase tracking-widest text-rd-dim font-mono">
            <div className="col-span-2">symbol · lane</div>
            <div className="col-span-2">bid</div>
            <div className="col-span-2">ask</div>
            <div className="col-span-2">last</div>
            <div className="col-span-2">spread</div>
            <div className="col-span-2">source · age</div>
          </div>
          {latest.map((row) => {
            const laneColor = LANE_COLOR[row.lane] || "#A1A1AA";
            const srcColor = SOURCE_COLOR[row.source] || "#A1A1AA";
            const rowKey = `${row.pair || "sym"}-${row.source || "src"}`;
            const cap =
              row.lane === "equity"
                ? cfg?.equity?.max_bps
                : cfg?.crypto?.max_bps;
            const overCap =
              typeof row.spread_bps === "number" &&
              typeof cap === "number" &&
              row.spread_bps > cap;
            return (
              <div
                key={rowKey}
                className="grid grid-cols-12 gap-2 px-2 py-1.5 items-center text-[11px] font-mono"
                data-testid={`spread-watcher-row-${rowKey}`}
              >
                <div className="col-span-2">
                  <div className="text-rd-text font-bold">{row.pair}</div>
                  <div className="uppercase tracking-widest text-[9px]" style={{ color: laneColor }}>
                    {row.lane || "—"}
                  </div>
                </div>
                <div className="col-span-2 text-rd-text">{fmtPrice(row.bid)}</div>
                <div className="col-span-2 text-rd-text">{fmtPrice(row.ask)}</div>
                <div className="col-span-2 text-rd-muted">{fmtPrice(row.last)}</div>
                <div className="col-span-2">
                  <span
                    className={overCap ? "text-rd-danger font-bold" : "text-rd-text"}
                    title={overCap ? `> ${cap} bps cap` : undefined}
                  >
                    {fmtBps(row.spread_bps)}
                  </span>
                </div>
                <div className="col-span-2 flex items-center gap-1.5">
                  <span
                    className="uppercase tracking-wider text-[9px]"
                    style={{ color: srcColor }}
                  >
                    {row.source}
                  </span>
                  <span className={row.stale ? "text-rd-warn" : "text-rd-muted"}>
                    {row.stale ? "STALE" : relTime(row.ts)}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

function GateChip({ label, on, bps, testid }) {
  return (
    <div className="flex items-center gap-1.5" data-testid={testid}>
      <span className="text-[10px] uppercase tracking-widest text-rd-dim">{label}</span>
      <Badge color={on ? "#10B981" : "#71717A"}>
        {on ? `ON @ ${Number(bps || 0).toFixed(0)}bps` : "OFF"}
      </Badge>
    </div>
  );
}
