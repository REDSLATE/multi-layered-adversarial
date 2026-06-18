import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { toast } from "sonner";
import {
  Eye, ArrowsClockwise, Target, TrendUp, TrendDown,
  CheckCircle, XCircle, Hourglass, Warning,
} from "@phosphor-icons/react";

/**
 * WebullOtocoLivePanel — operator tile that polls Webull's v3 open-
 * orders API and groups the rows into atomic OTOCO brackets so the
 * operator can watch the MASTER + TP + SL legs from Mission Control
 * instead of switching to the Webull mobile app.
 *
 * Polls every 8s. Pauses when the page is hidden (visibilitychange)
 * to avoid burning rate-limit credits when the tab isn't in front.
 */
const STATUS_COLOR = {
  WORKING: "#10B981",
  PENDING: "#F59E0B",
  SUBMITTED: "#3B82F6",
  PARTIALLY_FILLED: "#A78BFA",
  FILLED: "#10B981",
  CANCELLED: "#A1A1AA",
  REJECTED: "#DC2626",
  EXPIRED: "#A1A1AA",
};

function statusColor(status) {
  return STATUS_COLOR[String(status || "").toUpperCase()] || "#A1A1AA";
}

function LegPill({ kind, leg }) {
  if (!leg) {
    return (
      <div
        className="border border-rd-border bg-rd-bg/30 px-2 py-1.5 font-mono text-[10px] text-rd-dim"
        data-testid={`otoco-live-leg-${kind}-missing`}
      >
        <div className="uppercase tracking-widest">{kind}</div>
        <div className="italic">—</div>
      </div>
    );
  }
  const icon =
    kind === "master" ? Target :
    kind === "tp" ? TrendUp :
    kind === "sl" ? TrendDown : Hourglass;
  const Icon = icon;
  const price =
    leg.order_type === "LIMIT" ? leg.limit_price :
    leg.order_type === "STOP" ? leg.stop_price :
    null;
  return (
    <div
      className="border border-rd-border bg-rd-bg/60 px-2 py-1.5 font-mono text-[10px] space-y-0.5"
      data-testid={`otoco-live-leg-${kind}`}
    >
      <div className="flex items-center gap-1.5 uppercase tracking-widest">
        <Icon size={10} weight="bold" />
        <span>{kind}</span>
        <span
          className="ml-auto px-1.5 py-0.5"
          style={{ color: statusColor(leg.status), borderLeft: `2px solid ${statusColor(leg.status)}` }}
        >
          {leg.status || "?"}
        </span>
      </div>
      <div className="text-rd-text">
        <span className="text-rd-dim">{leg.order_type || "?"}</span>{" "}
        {price ? <span>@ ${Number(price).toFixed(2)}</span> : <span className="text-rd-dim">MKT</span>}
      </div>
      <div className="text-rd-dim truncate" title={leg.client_order_id}>
        {leg.client_order_id || leg.broker_order_id || "—"}
      </div>
    </div>
  );
}

function BracketCard({ bracket }) {
  return (
    <div
      className="border border-rd-accent/50 bg-rd-bg p-2 space-y-2"
      data-testid={`otoco-live-bracket-${bracket.combo_id}`}
    >
      <div className="flex items-center gap-2 text-[10px] font-mono">
        <span className="text-rd-accent uppercase tracking-widest">{bracket.symbol || "—"}</span>
        <span className="text-rd-dim truncate" title={bracket.combo_id}>
          combo · {bracket.combo_id}
        </span>
      </div>
      <div className="grid grid-cols-3 gap-1.5">
        <LegPill kind="master" leg={bracket.master} />
        <LegPill kind="tp" leg={bracket.tp} />
        <LegPill kind="sl" leg={bracket.sl} />
      </div>
      {bracket.other_legs && bracket.other_legs.length > 0 && (
        <div className="border-t border-rd-border pt-1">
          <div className="text-[9px] font-mono uppercase text-rd-dim mb-0.5 flex items-center gap-1">
            <Warning size={9} weight="bold" />
            unrecognized combo legs · {bracket.other_legs.length}
          </div>
          {bracket.other_legs.map((leg) => (
            <div key={leg.client_order_id || leg.broker_order_id} className="text-[10px] font-mono text-rd-text">
              {leg.order_type} · {leg.side} · {leg.status} · {leg.client_order_id}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function WebullOtocoLivePanel() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.get("/admin/webull/otoco/live");
      setData(res.data || null);
      setErr(null);
    } catch (e) {
      const detail = e?.response?.data?.detail || e.message;
      setErr(typeof detail === "string" ? detail : JSON.stringify(detail));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!autoRefresh) return;
    // Pause polling while tab is hidden to save rate-limit budget.
    let timer = null;
    const start = () => {
      if (timer) return;
      timer = setInterval(load, 8000);
    };
    const stop = () => {
      if (timer) { clearInterval(timer); timer = null; }
    };
    const onVis = () => {
      if (document.visibilityState === "visible") start();
      else stop();
    };
    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [autoRefresh, load]);

  const brackets = data?.brackets || [];
  const standalone = data?.standalone || [];
  const adapterMissing = data && data.ok === false;

  return (
    <div
      className="border border-rd-border bg-rd-bg2 p-3 space-y-3"
      data-testid="webull-otoco-live-panel"
    >
      <div className="flex items-center gap-2">
        <Eye size={13} weight="bold" className="text-rd-accent" />
        <span className="text-[10px] font-mono uppercase tracking-widest text-rd-text">
          Live OTOCO Orders
        </span>
        <span className="text-[10px] font-mono text-rd-dim">
          · open: {data?.open_count ?? "?"}
          {brackets.length > 0 && ` · brackets: ${brackets.length}`}
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          <button
            onClick={() => setAutoRefresh((v) => !v)}
            className={
              "px-2 py-0.5 text-[9px] font-mono uppercase tracking-wider border font-bold " +
              (autoRefresh
                ? "border-rd-success bg-rd-success text-black shadow-[0_0_10px_rgba(34,197,94,0.55)]"
                : "border-rd-border text-rd-dim")
            }
            data-testid="otoco-live-autorefresh"
            title="auto-refresh every 8s"
          >
            {autoRefresh ? "● LIVE · 8s" : "paused"}
          </button>
          <button
            onClick={load}
            disabled={loading}
            className="p-1 border border-rd-border text-rd-dim hover:text-rd-text"
            title="Reload now"
            data-testid="otoco-live-reload"
          >
            <ArrowsClockwise size={10} weight="bold" />
          </button>
        </div>
      </div>

      {adapterMissing && (
        <div className="text-[10px] font-mono text-rd-dim italic flex items-center gap-1.5">
          <Warning size={10} weight="bold" />
          Webull adapter not configured — set WEBULL credentials in backend/.env.
        </div>
      )}

      {err && (
        <div className="border border-rd-danger bg-rd-bg p-2 font-mono text-[10px] text-rd-danger flex items-start gap-1.5">
          <XCircle size={11} weight="bold" className="mt-0.5 shrink-0" />
          <span className="flex-1">{err}</span>
        </div>
      )}

      {!adapterMissing && !err && brackets.length === 0 && standalone.length === 0 && (
        <div className="text-[10px] font-mono text-rd-dim italic">
          No open Webull orders. Fire an OTOCO above to populate this tile.
        </div>
      )}

      {brackets.length > 0 && (
        <div className="space-y-2" data-testid="otoco-live-brackets">
          {brackets.map((b) => (
            <BracketCard key={b.combo_id} bracket={b} />
          ))}
        </div>
      )}

      {standalone.length > 0 && (
        <div className="border-t border-rd-border pt-2 space-y-1">
          <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim flex items-center gap-1">
            <CheckCircle size={9} weight="bold" />
            standalone open orders · {standalone.length}
          </div>
          {standalone.map((o, i) => (
            <div
              key={o.client_order_id || i}
              className="border border-rd-border bg-rd-bg/60 px-2 py-1 font-mono text-[10px] flex items-center gap-2"
              data-testid={`otoco-live-standalone-${i}`}
            >
              <span className="text-rd-text font-bold">{o.symbol || "?"}</span>
              <span className="text-rd-dim">{o.side}</span>
              <span className="text-rd-dim">{o.order_type}</span>
              <span className="ml-auto" style={{ color: statusColor(o.status) }}>
                {o.status}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
