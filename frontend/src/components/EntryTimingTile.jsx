import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Timer, Broadcast } from "@phosphor-icons/react";

const Stat = ({ label, value, suffix = "", tone = "text-rd-text", testid }) => (
  <div className="border border-rd-border px-3 py-1.5" data-testid={testid}>
    <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">{label}</div>
    <div className={`font-display text-lg font-bold leading-none ${tone}`}>
      {value ?? "—"}{value != null ? suffix : ""}
    </div>
  </div>
);

const OUTCOME_META = {
  waiting: ["waiting", "text-rd-muted"],
  blocked_again: ["blocked again", "text-amber-500"],
  submitted: ["submitted", "text-sky-400"],
  filled: ["filled", "text-rd-success"],
  broker_rejected: ["broker rejected", "text-red-500"],
  reconciliation_required: ["reconcile!", "text-red-500"],
};

const Row = ({ k, v }) => (
  <div className="flex gap-2 text-[10px] font-mono">
    <span className="text-rd-dim w-44 shrink-0">{k}</span>
    <span className="text-rd-text break-all">{v ?? "—"}</span>
  </div>
);

const TimelineView = ({ tl }) => {
  if (!tl?.found) return (
    <div className="text-[10px] font-mono text-rd-dim" data-testid="et-timeline-empty">
      no qualifying organic re-arm yet
    </div>
  );
  const o = tl.original_intent || {}, p = tl.pullback || {}, c = tl.child || {};
  const et2 = typeof c.second_entry_timing === "object" ? c.second_entry_timing : null;
  return (
    <div className="space-y-0.5 max-h-80 overflow-y-auto pr-1" data-testid="et-rearm-timeline">
      <div className="text-[9px] font-mono uppercase tracking-widest text-amber-500 mt-1">1 · Original intent + block</div>
      <Row k="intent id" v={o.intent_id} />
      <Row k="confirmation" v={o.confirmation_price != null ? `${o.confirmation_price} (${o.confirmation_source})` : null} />
      <Row k="block reason" v={o.block_reason} />
      <Row k="extension vs cap" v={o.extension_pct != null ? `${o.extension_pct}% vs ${o.class_cap_pct}% (${o.universe_class})` : null} />
      <div className="text-[9px] font-mono uppercase tracking-widest text-amber-500 mt-1.5">2 · Watch + pullback</div>
      <Row k="trigger id" v={tl.trigger?.trigger_id} />
      <Row k="state" v={`${tl.trigger?.state} · ${tl.trigger?.state_reason || ""} @ ${tl.trigger?.state_ts || ""}`} />
      <Row k="pullback depth" v={p.depth_pct != null ? `${p.depth_pct}%` : null} />
      <Row k="volume contraction" v={p.volume_contraction} />
      <Row k="support" v={p.support ? `ema9 ${p.support.ema9 ?? "—"} · vwap ${p.support.vwap ?? "—"}` : null} />
      <Row k="reacceleration" v={p.reacceleration} />
      <div className="text-[9px] font-mono uppercase tracking-widest text-amber-500 mt-1.5">3 · Re-armed child</div>
      <Row k="child intent id" v={c.intent_id} />
      <Row k="new confirmation" v={c.new_confirmation_price} />
      <Row k="new invalidation/stop" v={c.new_invalidation_price} />
      <Row k="entry improvement" v={tl.improvement_pct != null ? `${tl.improvement_pct}% vs blocked price` : null} />
      <Row k="in local queue" v={String(tl.queue?.in_local_queue)} />
      <div className="text-[9px] font-mono uppercase tracking-widest text-amber-500 mt-1.5">4 · Second gate chain</div>
      <Row k="seat tier" v={c.seat_tier} />
      <Row k="risk result" v={c.risk_reason || (c.gate_state === "submitted" || c.executed ? "passed" : c.gate_state)} />
      <Row k="allowlist" v={c.allowlist} />
      <Row k="entry timing #2" v={et2 ? `${et2.decision} · ${et2.reason} · ext ${et2.extension_pct ?? "—"}% · fresh ${et2.fresh_price ?? "—"}` : c.second_entry_timing} />
      <Row k="outcome" v={c.outcome} />
      <div className="text-[9px] font-mono uppercase tracking-widest text-amber-500 mt-1.5">5 · Broker</div>
      {(tl.broker?.executions || []).length === 0 && <Row k="executions" v="none" />}
      {(tl.broker?.executions || []).map((e, i) => (
        <Row key={i} k={`submit ${i + 1}`} v={`ok=${e.ok} status=${e.broker_status ?? "—"} $${e.notional_usd ?? "—"} ${e.exception_msg || e.risk_reason || ""}`} />
      ))}
      {(tl.broker?.fills || []).map((f, i) => (
        <Row key={`f${i}`} k={`fill ${i + 1}`} v={`${f.qty} @ ${f.price} fee ${f.fee ?? "—"} (${f.broker}) order ${f.order_id ?? "—"}`} />
      ))}
    </div>
  );
};

/** Entry Timing tile — chase protection + Prod Deploy Watch:
 *  first-organic-re-arm event, child outcomes, P0 health guards. */
export const EntryTimingTile = () => {
  const [win, setWin] = useState("24h");
  const [data, setData] = useState(null);
  const [first, setFirst] = useState(null);
  const [health, setHealth] = useState(null);
  const [tl, setTl] = useState(null);
  const [showTl, setShowTl] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () => {
      api.get("/admin/universe/entry-timing/stats")
        .then(({ data: d }) => {
          if (!alive) return;
          setData(d.windows);
          setFirst(d.first_prod_rearm || null);
        })
        .catch(() => {});
      api.get("/admin/universe/entry-timing/health")
        .then(({ data: d }) => alive && setHealth(d))
        .catch(() => {});
    };
    load();
    const t = setInterval(load, 60000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const openTimeline = () => {
    const next = !showTl;
    setShowTl(next);
    if (next && !tl) {
      api.get("/admin/universe/entry-timing/rearm-timeline")
        .then(({ data: d }) => setTl(d))
        .catch(() => setTl({ found: false }));
    }
  };

  const w = data?.[win];
  const alerts = (health?.checks || []).filter((c) => !c.ok);
  const outcomes = w?.rearm_children?.outcomes || {};
  return (
    <div className="border border-rd-border bg-rd-panel p-3" data-testid="entry-timing-tile">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <Timer size={14} weight="bold" className="text-amber-500" />
          <span className="text-xs font-mono font-bold uppercase tracking-widest text-rd-text">
            Entry Timing
          </span>
        </div>
        <div className="flex gap-1">
          {["24h", "7d"].map((k) => (
            <button
              key={k}
              onClick={() => setWin(k)}
              className={`px-2 py-0.5 text-[10px] font-mono uppercase border transition-colors ${
                win === k ? "border-rd-text text-rd-text" : "border-rd-border text-rd-dim hover:text-rd-muted"
              }`}
              data-testid={`entry-timing-window-${k}`}
            >
              {k}
            </button>
          ))}
        </div>
      </div>

      {first && (
        <button
          onClick={openTimeline}
          className="w-full mb-2 border border-rd-success bg-rd-success/10 px-3 py-2 text-left hover:bg-rd-success/20 transition-colors"
          data-testid="et-first-rearm-banner"
        >
          <div className="flex items-center gap-2">
            <Broadcast size={14} weight="bold" className="text-rd-success animate-pulse" />
            <span className="text-[11px] font-mono font-bold uppercase tracking-widest text-rd-success">
              First prod re-arm · {first.symbol}
            </span>
            <span className="ml-auto text-[10px] font-mono text-rd-muted">
              {OUTCOME_META[first.outcome]?.[0] || first.outcome} · {showTl ? "hide" : "view"} timeline
            </span>
          </div>
        </button>
      )}
      {showTl && <div className="mb-2 border border-rd-border p-2"><TimelineView tl={tl} /></div>}

      {alerts.length > 0 && (
        <div className="mb-2 border border-red-500 bg-red-500/10 px-2 py-1.5 space-y-0.5" data-testid="et-health-alerts">
          {alerts.map((a) => (
            <div key={a.id} className="text-[10px] font-mono text-red-400" data-testid={`et-health-${a.id}`}>
              ⚠ P0 GUARD [{a.id}]: {a.detail}
            </div>
          ))}
        </div>
      )}

      {!w ? (
        <div className="text-[10px] font-mono text-rd-dim">loading…</div>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-2 mb-2">
            <Stat label="fired" value={w.entries_fired} testid="et-fired" />
            <Stat label="blocked late" value={w.late_entries_blocked} tone="text-amber-500" testid="et-blocked" />
            <Stat label="re-armed" value={w.triggers?.rearmed} tone="text-rd-success" testid="et-rearmed" />
            <Stat label="re-arm filled" value={w.rearmed_filled} tone="text-rd-success" testid="et-rearm-filled" />
            <Stat label="ran w/o pullback" value={w.triggers?.expired} testid="et-expired" />
            <Stat label="broke down" value={w.triggers?.invalidated} tone="text-red-500" testid="et-invalidated" />
          </div>
          {w.rearm_children?.created > 0 && (
            <div className="flex flex-wrap gap-1 mb-2" data-testid="et-rearm-outcomes">
              {Object.entries(OUTCOME_META).map(([k, [label, tone]]) => (
                <span
                  key={k}
                  className={`border border-rd-border px-1.5 py-0.5 text-[9px] font-mono uppercase ${outcomes[k] ? tone : "text-rd-dim"}`}
                  data-testid={`et-rearm-${k}`}
                >
                  {label} {outcomes[k] ?? 0}
                </span>
              ))}
              <span className="border border-rd-border px-1.5 py-0.5 text-[9px] font-mono uppercase text-rd-muted" data-testid="et-rearm-queue">
                queued {w.rearm_children.in_local_queue}/{w.rearm_children.created} · routed {w.rearm_children.routed}
              </span>
            </div>
          )}
          <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-[10px] font-mono text-rd-muted">
            <span>avg extension at fill: <b className="text-rd-text">{w.avg_extension_at_fill_pct ?? "—"}%</b></span>
            <span>re-entry improvement: <b className="text-rd-success">{w.avg_reentry_improvement_pct ?? "—"}%</b></span>
            <span>chase avoided: <b className="text-rd-success">{w.chase_avoided_avg_pct ?? "—"}%</b></span>
            <span>missed by waiting: <b className="text-amber-500">{w.missed_by_waiting_avg_pct ?? "—"}%</b></span>
          </div>
          <div className="mt-1.5 text-[9px] font-mono text-rd-dim" data-testid="et-footer">
            watching {w.triggers?.watching ?? 0} · organic BUYs {w.organic_buy_intents ?? 0} · conf src{" "}
            {Object.entries(w.confirmation_sources || {}).map(([k, v]) => `${k}:${v}`).join(" ") || "—"}{" "}
            · dup suppressed {w.duplicate_suppressions ?? 0}
            {health?.ok && <span className="text-rd-success"> · P0 guards OK</span>}
          </div>
        </>
      )}
    </div>
  );
};

export default EntryTimingTile;
