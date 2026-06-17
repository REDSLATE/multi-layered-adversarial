/**
 * Funnel deltas tile — 2026-02-20 operator pin
 *
 * "Not for convenience; for proof. Right after deploy, the question is
 *  simple: did the doctrine patch change behavior?"
 *
 * READ-ONLY. No toggles, no actions. Polls
 * GET /api/admin/intents/funnel-deltas every 30s and renders the
 * comparative funnel between the last 24h and the prior 24h:
 *
 *   HOLD% ↓     (good: brains emitting fewer HOLDs)
 *   BUY/SELL% ↑ (good: directional flow recovered)
 *   C_QUALITY ↑ (good: toehold trades appearing)
 *   Submitted ↑ (good: orders reaching the broker)
 *   RoadGuard/Broker rejects flat (proof the change was upstream of execution)
 *
 * Arrow direction + color is derived from whether the delta is moving
 * in the "predicted" direction for the post-doctrine-patch state.
 * No P&L. No money. Just funnel mechanics.
 */
import { useEffect, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  ArrowsLeftRight,
  CheckCircle,
  Warning,
} from "@phosphor-icons/react";
import { api } from "@/lib/api";

const POLL_MS = 30_000;


function DeltaRow({ label, delta, unit, goodDirection, testid }) {
  // goodDirection: "down" | "up" | "flat"
  // null/0 deltas render as ArrowsLeftRight (no change).
  const num = typeof delta === "number" ? delta : null;
  let icon = <ArrowsLeftRight size={12} className="text-rd-dim" />;
  let color = "text-rd-dim";
  if (num !== null && num !== 0) {
    if (num > 0) {
      icon = <ArrowUp size={12} />;
      // Up is "good" iff predicted direction is up. Flat-expected
      // shows neutral color when small, warning when large.
      if (goodDirection === "up") color = "text-rd-ok";
      else if (goodDirection === "down") color = "text-rd-bad";
      else color = Math.abs(num) > 5 ? "text-rd-warn" : "text-rd-dim";
    } else {
      icon = <ArrowDown size={12} />;
      if (goodDirection === "down") color = "text-rd-ok";
      else if (goodDirection === "up") color = "text-rd-bad";
      else color = Math.abs(num) > 5 ? "text-rd-warn" : "text-rd-dim";
    }
  }
  const formatted =
    num === null
      ? "—"
      : `${num > 0 ? "+" : ""}${num.toFixed(unit === "pp" ? 1 : 0)}${unit}`;
  return (
    <div className="flex items-center justify-between gap-2 font-mono text-[10px]" data-testid={testid}>
      <span className="text-rd-dim">{label}</span>
      <span className={`flex items-center gap-0.5 ${color} tabular-nums`}>
        {icon}
        {formatted}
      </span>
    </div>
  );
}


export default function FunnelDeltasTile() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let alive = true;

    async function fetchOnce() {
      try {
        const { data: json } = await api.get("/admin/intents/funnel-deltas");
        if (alive) {
          setData(json);
          setErr(null);
        }
      } catch (e) {
        if (alive) setErr(e.message || String(e));
      }
    }

    fetchOnce();
    const t = setInterval(fetchOnce, POLL_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  if (err && !data) {
    return (
      <div className="border border-rd-bad bg-rd-bad/5 p-2 font-mono text-[10px] text-rd-bad" data-testid="funnel-deltas-error">
        funnel-deltas fetch failed: {err}
      </div>
    );
  }
  if (!data) {
    return (
      <div className="border border-rd-border bg-rd-bg p-2 font-mono text-[10px] text-rd-dim" data-testid="funnel-deltas-loading">
        loading funnel deltas…
      </div>
    );
  }

  const d = data.deltas || {};
  const interp = data.interpretation || {};
  const healthy = Boolean(interp.healthy);
  const cur = data.windows?.current?.metrics || {};
  const base = data.windows?.baseline?.metrics || {};

  return (
    <div
      className={`border p-2.5 space-y-2 ${
        healthy ? "border-rd-ok bg-rd-ok/5" : "border-rd-border bg-rd-bg"
      }`}
      data-testid="funnel-deltas-tile"
    >
      <div className="flex items-center gap-2">
        {healthy ? (
          <CheckCircle size={13} weight="bold" className="text-rd-ok" />
        ) : (
          <Warning size={13} weight="bold" className="text-rd-dim" />
        )}
        <div className="flex-1">
          <div className="font-mono text-[11px] uppercase tracking-widest text-rd-text font-bold">
            Funnel deltas · 24h vs prior 24h
          </div>
          <div className="font-mono text-[9px] text-rd-dim mt-0.5">
            Did the doctrine patch change behavior? Read-only proof.
            {cur.total_intents !== undefined && base.total_intents !== undefined && (
              <>
                {" · "}current={cur.total_intents} · baseline={base.total_intents}
              </>
            )}
          </div>
        </div>
      </div>

      {/* Two-column delta grid. Left = action distribution + quality;
          right = execution proof (submitted + rejects-should-be-flat). */}
      <div className="grid grid-cols-2 gap-x-4 gap-y-1">
        <div className="space-y-1">
          <DeltaRow
            label="HOLD%"
            delta={d.hold_pct}
            unit="pp"
            goodDirection="down"
            testid="funnel-delta-hold"
          />
          <DeltaRow
            label="BUY%"
            delta={d.buy_pct}
            unit="pp"
            goodDirection="up"
            testid="funnel-delta-buy"
          />
          <DeltaRow
            label="SELL%"
            delta={d.sell_pct}
            unit="pp"
            goodDirection="up"
            testid="funnel-delta-sell"
          />
          <DeltaRow
            label="REJECT count"
            delta={d.reject_count}
            unit=""
            goodDirection="down"
            testid="funnel-delta-reject"
          />
          <DeltaRow
            label="C_QUALITY count"
            delta={d.c_quality_count}
            unit=""
            goodDirection="up"
            testid="funnel-delta-cquality"
          />
        </div>
        <div className="space-y-1">
          <DeltaRow
            label="Submitted"
            delta={d.submitted_count}
            unit=""
            goodDirection="up"
            testid="funnel-delta-submitted"
          />
          <DeltaRow
            label="B_QUALITY count"
            delta={d.b_quality_count}
            unit=""
            goodDirection="up"
            testid="funnel-delta-bquality"
          />
          <DeltaRow
            label="A_QUALITY count"
            delta={d.a_quality_count}
            unit=""
            goodDirection="up"
            testid="funnel-delta-aquality"
          />
          <DeltaRow
            label="RoadGuard rejects"
            delta={d.roadguard_count}
            unit=""
            goodDirection="flat"
            testid="funnel-delta-roadguard"
          />
          <DeltaRow
            label="Broker rejects"
            delta={d.broker_count}
            unit=""
            goodDirection="flat"
            testid="funnel-delta-broker"
          />
        </div>
      </div>

      {/* Interpretation notes. Operator-readable summary of which way
          the funnel moved + any anomalies. */}
      {Array.isArray(interp.notes) && interp.notes.length > 0 && (
        <div className="border-t border-rd-border pt-1.5 space-y-0.5 font-mono text-[9px]" data-testid="funnel-deltas-notes">
          {interp.notes.map((note, i) => (
            <div
              key={i}
              className={note.startsWith("⚠") ? "text-rd-warn" : "text-rd-dim"}
            >
              {note}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
