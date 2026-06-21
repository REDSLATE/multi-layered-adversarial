/**
 * IntentFunnelTile — 7-stage execution funnel (2026-02-21)
 *
 * Operator pin: "Right after we deploy, I want to see at one glance
 * exactly where the funnel leaks. Brains say BUY — but somewhere
 * between Brain and Broker, intents die. Show me where."
 *
 * Polls GET /api/admin/intents/funnel?hours=24 every 30s and renders
 * a horizontal stage list with:
 *   * count + % of emitted
 *   * drop-from-previous (count + %) so the operator sees the leak
 *   * visual emphasis on the BIGGEST drop (red border)
 *   * an explicit "biggest leak" sentence under the bars
 *
 * Stages (top → bottom): Brain → Seat → Governor → RoadGuard →
 *                        AutoSubmit → Broker → Fill
 *
 * READ-ONLY. No toggles, no actions.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowsClockwise, ArrowsLeftRight, FunnelSimple, Warning, XCircle } from "@phosphor-icons/react";
import { toast } from "sonner";
import { api } from "@/lib/api";

const POLL_MS = 30_000;
const WINDOWS = [1, 6, 24, 72];


function StageBar({ stage, totalEmitted, isBiggestDrop }) {
  const pct = totalEmitted > 0 ? (100 * stage.count) / totalEmitted : 0;
  // Bar width scales with pct (0–100).
  const widthStyle = { width: `${Math.max(2, Math.min(100, pct))}%` };
  return (
    <div
      className={
        "border bg-rd-bg p-1.5 space-y-1 " +
        (isBiggestDrop ? "border-rd-danger border-2" : "border-rd-border")
      }
      data-testid={`funnel-stage-${stage.key}`}
    >
      <div className="flex items-baseline justify-between font-mono text-[10px]">
        <span className="text-rd-text uppercase tracking-wider">
          {stage.name}
        </span>
        <span className="text-rd-dim">
          <span className="text-rd-text font-bold">{stage.count}</span>
          {" · "}
          {stage.pct_of_total.toFixed(1)}%
          {stage.drop_from_prev > 0 && (
            <span className={isBiggestDrop ? "text-rd-danger ml-1.5" : "text-rd-warn ml-1.5"}>
              −{stage.drop_from_prev} ({stage.drop_pct.toFixed(0)}%)
            </span>
          )}
        </span>
      </div>
      <div className="h-1.5 bg-rd-bg2 relative overflow-hidden">
        <div
          className={isBiggestDrop ? "h-full bg-rd-danger" : "h-full bg-rd-accent"}
          style={widthStyle}
        />
      </div>
    </div>
  );
}


export default function IntentFunnelTile() {
  const [hours, setHours] = useState(24);
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);
  // Remember the last shift signature we've toasted on so we don't
  // re-fire the same toast on every 30s poll.
  const lastShiftSigRef = useRef(null);

  const load = useCallback(async (h) => {
    setLoading(true);
    try {
      const res = await api.get(`/admin/intents/funnel?hours=${h}`);
      setData(res.data);
      setErr(null);
      // Surface a non-blocking toast the first time we see a new
      // stage shift. The banner stays visible inside the tile too.
      const shift = res.data?.stage_shift;
      if (shift) {
        const sig = `${h}:${shift.from_stage}>${shift.to_stage}:${shift.prev_captured_at}`;
        if (lastShiftSigRef.current !== sig) {
          lastShiftSigRef.current = sig;
          toast.warning(
            `Funnel leak shifted: ${shift.from_stage} → ${shift.to_stage}`,
            { description: `Last seen at ${shift.from_stage} ${shift.gap_seconds}s ago. New bug?`, duration: 12000 },
          );
        }
      }
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setErr(typeof d === "string" ? d : JSON.stringify(d));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(hours);
    const id = setInterval(() => load(hours), POLL_MS);
    return () => clearInterval(id);
  }, [load, hours]);

  const biggestDropTo = data?.biggest_drop?.to || null;

  return (
    <div
      className="border-2 border-rd-accent/60 bg-rd-bg2 p-2.5 space-y-2"
      data-testid="intent-funnel-tile"
    >
      <div className="flex items-center gap-2">
        <FunnelSimple size={13} weight="bold" className="text-rd-accent" />
        <div className="flex-1">
          <div
            className="font-mono text-[11px] uppercase tracking-widest text-rd-text font-bold"
            data-testid="funnel-tile-title"
          >
            Execution funnel · Brain → Seat → Governor → RoadGuard → AutoSubmit → Broker → Fill
          </div>
          <div className="font-mono text-[9px] text-rd-dim mt-0.5">
            Where do intents die between emission and fill? Biggest leak is outlined.
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          {WINDOWS.map((h) => (
            <button
              key={h}
              onClick={() => setHours(h)}
              className={
                "px-2 py-0.5 font-mono text-[10px] uppercase border " +
                (hours === h
                  ? "border-rd-accent text-rd-accent"
                  : "border-rd-border text-rd-dim hover:text-rd-text")
              }
              data-testid={`funnel-window-${h}h`}
            >
              {h}h
            </button>
          ))}
          <button
            onClick={() => load(hours)}
            disabled={loading}
            className="p-1 border border-rd-border text-rd-dim hover:text-rd-text"
            data-testid="funnel-reload"
            title="Reload now"
          >
            <ArrowsClockwise size={11} weight="bold" className={loading ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {err && (
        <div
          className="border border-rd-danger bg-rd-bg p-2 font-mono text-[10px] text-rd-danger flex items-start gap-1.5"
          data-testid="funnel-error"
        >
          <XCircle size={11} weight="bold" className="mt-0.5 shrink-0" />
          {err}
        </div>
      )}

      {data && (
        <>
          {data.stage_shift && (
            <div
              className="border-2 border-rd-warn bg-rd-warn/10 p-2 font-mono text-[11px] text-rd-warn flex items-start gap-1.5"
              data-testid="funnel-stage-shift"
            >
              <ArrowsLeftRight size={12} weight="bold" className="mt-0.5 shrink-0" />
              <span>
                <span className="font-bold uppercase tracking-wider">Leak shifted:</span>{" "}
                <span className="font-bold">{data.stage_shift.from_stage}</span>
                {" → "}
                <span className="font-bold">{data.stage_shift.to_stage}</span>
                <span className="text-rd-dim">
                  {" — was at "}
                  <span className="font-bold">{data.stage_shift.from_stage}</span>{" "}
                  {data.stage_shift.gap_seconds}s ago. New bug?
                </span>
              </span>
            </div>
          )}

          <div className="grid grid-cols-3 gap-2 text-center" data-testid="funnel-headline">
            <div className="border border-rd-border bg-rd-bg p-1.5">
              <div className="font-mono text-[9px] uppercase text-rd-dim">Emitted</div>
              <div className="font-mono text-lg text-rd-text">{data.total_intents}</div>
            </div>
            <div className="border border-rd-border bg-rd-bg p-1.5">
              <div className="font-mono text-[9px] uppercase text-rd-dim">Filled</div>
              <div
                className="font-mono text-lg"
                style={{
                  color:
                    data.stages.find((s) => s.key === "filled")?.count > 0
                      ? "#10B981"
                      : "#DC2626",
                }}
              >
                {data.stages.find((s) => s.key === "filled")?.count ?? 0}
              </div>
            </div>
            <div className="border border-rd-border bg-rd-bg p-1.5">
              <div className="font-mono text-[9px] uppercase text-rd-dim">Fill rate</div>
              <div className="font-mono text-lg text-rd-text">
                {data.total_intents > 0
                  ? (
                      (100 * (data.stages.find((s) => s.key === "filled")?.count ?? 0)) /
                      data.total_intents
                    ).toFixed(1) + "%"
                  : "—"}
              </div>
            </div>
          </div>

          {data.biggest_drop && (
            <div
              className="border border-rd-warn bg-rd-warn/5 p-2 font-mono text-[11px] text-rd-warn flex items-start gap-1.5"
              data-testid="funnel-biggest-drop"
            >
              <Warning size={11} weight="bold" className="mt-0.5 shrink-0" />
              <span>
                Biggest leak: <span className="font-bold">{data.biggest_drop.lost}</span> intents
                ({data.biggest_drop.drop_pct.toFixed(0)}%) dropped between{" "}
                <span className="font-bold">{data.biggest_drop.from}</span> and{" "}
                <span className="font-bold">{data.biggest_drop.to}</span>.
              </span>
            </div>
          )}

          <div className="space-y-1" data-testid="funnel-stages">
            {data.stages.map((s) => (
              <StageBar
                key={s.key}
                stage={s}
                totalEmitted={data.total_intents}
                isBiggestDrop={biggestDropTo === s.name && s.drop_from_prev > 0}
              />
            ))}
          </div>

          {data.no_receipt_count > 0 && (
            <div
              className="font-mono text-[9px] text-rd-dim border-t border-rd-border pt-1"
              data-testid="funnel-no-receipt-note"
            >
              {data.no_receipt_count} intent
              {data.no_receipt_count === 1 ? "" : "s"} had no pipeline_receipt
              (legacy/non-unified path) — Filled count still reflects broker
              confirmation.
            </div>
          )}

          {(Object.keys(data.by_lane || {}).length > 0 ||
            Object.keys(data.by_brain || {}).length > 0) && (
            <details className="font-mono text-[10px]">
              <summary className="cursor-pointer text-rd-dim hover:text-rd-text">
                Breakdown by lane + brain ▾
              </summary>
              <div className="grid grid-cols-2 gap-2 mt-1">
                <div data-testid="funnel-by-lane">
                  <div className="text-rd-dim text-[9px] uppercase mb-0.5">By lane</div>
                  {Object.entries(data.by_lane || {}).map(([lane, counts]) => (
                    <div key={lane} className="border border-rd-border p-1 mb-1">
                      <div className="text-rd-text font-bold uppercase">{lane}</div>
                      <div className="text-rd-dim text-[9px]">
                        {counts.emitted} emitted · {counts.seat_approved} seat · {counts.roadguard_passed} RG · {counts.broker_accepted} accepted · {counts.filled} filled
                      </div>
                    </div>
                  ))}
                </div>
                <div data-testid="funnel-by-brain">
                  <div className="text-rd-dim text-[9px] uppercase mb-0.5">By brain</div>
                  {Object.entries(data.by_brain || {}).map(([brain, counts]) => (
                    <div key={brain} className="border border-rd-border p-1 mb-1">
                      <div className="text-rd-text font-bold uppercase">{brain}</div>
                      <div className="text-rd-dim text-[9px]">
                        {counts.emitted} emitted · {counts.seat_approved} seat · {counts.roadguard_passed} RG · {counts.broker_accepted} accepted · {counts.filled} filled
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </details>
          )}
        </>
      )}
    </div>
  );
}
