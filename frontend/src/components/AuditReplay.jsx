import React, { useState } from "react";
import { api } from "@/lib/api";
import { Badge } from "@/components/ui-bits";
import { ClockCounterClockwise, ArrowsClockwise } from "@phosphor-icons/react";

/**
 * Audit-replay strip. When an opinion carries `evidence.technical_ref`,
 * we render a one-line "Camaro saw" preview of the snapshot AS-OF the
 * moment Camaro read it, recomputed from retained bars. Doctrine
 * payoff: every quantitative call is later reproducible from raw data.
 */
export default function AuditReplay({ technicalRef, quotedValues }) {
  const [open, setOpen] = useState(false);
  const [snap, setSnap] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  if (!technicalRef || !technicalRef.symbol || !technicalRef.tf) return null;

  const { symbol, tf, source, computed_at } = technicalRef;
  const asOf = computed_at;

  const replay = async () => {
    if (snap) {
      setOpen(!open);
      return;
    }
    setBusy(true);
    setErr("");
    try {
      const { data } = await api.get(
        `/shared/technical/${encodeURIComponent(symbol)}`,
        { params: { tf, source, as_of: asOf } },
      );
      setSnap(data);
      setOpen(true);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  };

  const ind = snap?.snapshot?.indicators || null;

  return (
    <div className="mt-2" data-testid="audit-replay">
      <button
        type="button"
        onClick={replay}
        disabled={busy}
        className="text-[10px] uppercase tracking-widest text-rd-dim hover:text-rd-text flex items-center gap-1 font-mono"
      >
        <ClockCounterClockwise size={10} weight="bold" />
        {busy ? "replaying…" : (open ? "hide replay" : "replay technical evidence")}
        <span className="text-rd-muted normal-case tracking-normal ml-1">
          · {symbol} {tf} @ {asOf ? new Date(asOf).toLocaleString() : "—"}
        </span>
      </button>
      {err && (
        <div className="mt-1.5 text-[10px] font-mono text-rd-danger">
          replay failed: {err}
        </div>
      )}
      {open && ind && (
        <div className="mt-1.5 border border-rd-border bg-rd-bg2 p-2 text-[10px] font-mono">
          <div className="flex items-center gap-2 mb-1.5">
            <Badge color="#A1A1AA">AUDIT REPLAY</Badge>
            <span className="text-rd-dim">
              recomputed from {ind.bars_seen} bars · last bar{" "}
              {snap.snapshot.last_bar_ts ? new Date(snap.snapshot.last_bar_ts).toLocaleString() : "—"}
            </span>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-1.5">
            <Cell label="Close"      saw={ind.last_close}                       quoted={null} />
            <Cell label="RSI(14)"    saw={ind.rsi14}                            quoted={quotedValues?.rsi14} digits={1} />
            <Cell label="MACD hist"  saw={ind?.macd?.hist}                      quoted={quotedValues?.macd_hist} digits={3} signed />
            <Cell label="BB pos"     saw={pct(ind?.bbands?.position)}           quoted={quotedValues?.bb_position != null ? pct(quotedValues.bb_position) : null} suffix="%" />
            <Cell label="SMA 20"     saw={ind?.sma?.["20"]}                     quoted={null} />
            <Cell label="SMA 50"     saw={ind?.sma?.["50"]}                     quoted={null} />
            <Cell label="SMA 200"    saw={ind?.sma?.["200"]}                    quoted={null} />
            <Cell label="ATR%"       saw={ind.atr14_pct}                        quoted={null} digits={2} suffix="%" />
          </div>
          {quotedValues && Object.keys(quotedValues).length > 0 && (
            <div className="mt-2 pt-1.5 border-t border-rd-border text-[10px] text-rd-muted">
              Highlighted cells = values the brain explicitly quoted in <code>evidence.values</code>.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Cell({ label, saw, quoted, digits = 2, signed = false, suffix = "" }) {
  const isQuoted = quoted != null;
  return (
    <div
      className={`border ${isQuoted ? "border-rd-text bg-rd-bg3" : "border-rd-border"} px-1.5 py-1`}
    >
      <div className="text-[9px] uppercase tracking-widest text-rd-dim">{label}</div>
      <div className={isQuoted ? "text-rd-text" : "text-rd-muted"}>
        {fmt(saw, digits, signed)}{suffix}
      </div>
      {isQuoted && (
        <div className="text-[9px] text-rd-dim">
          quoted {fmt(quoted, digits, signed)}{suffix}
        </div>
      )}
    </div>
  );
}

function pct(v) {
  if (v == null) return null;
  return Number(v) * 100;
}

function fmt(v, digits = 2, signed = false) {
  if (v == null || isNaN(v)) return "—";
  const n = Number(v);
  if (Math.abs(n) >= 10000) return n.toFixed(0);
  const out = n.toFixed(digits);
  return signed && n > 0 ? `+${out}` : out;
}
