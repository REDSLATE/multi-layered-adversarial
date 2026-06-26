/**
 * InputProvenanceBadge — per-intent "was this based on fresh data?" dot.
 *
 * Reads `intent.evidence.input_provenance` (stamped by the native
 * runner at emit time, see `shared/brains/_runner_core.py`). For
 * legacy intents that predate the migration, no provenance exists
 * and we show a neutral "?" badge.
 *
 * Doctrine: matches the trust bands in `admin_brain_input_health`
 * so the dot here and the BIH tile thresholds agree exactly.
 *
 *   fresh      green   — snapshot < 10min old, ≥ 60 bars, all fields
 *   stale      red     — snapshot > 10min old
 *   thin_bars  amber   — bars < 60 (indicator math statistically thin)
 *   unknown    slate   — legacy intent without stamped provenance
 */
import { useState } from "react";

const TONE = {
  fresh:     { dot: "bg-emerald-400", text: "text-emerald-300", label: "fresh" },
  stale:     { dot: "bg-red-400",     text: "text-red-300",     label: "stale" },
  thin_bars: { dot: "bg-amber-400",   text: "text-amber-300",   label: "thin bars" },
  unknown:   { dot: "bg-slate-500",   text: "text-slate-400",   label: "no provenance" },
};

function fmtAge(sec) {
  if (sec == null) return "—";
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  if (sec < 86400) return `${(sec / 3600).toFixed(1)}h`;
  return `${(sec / 86400).toFixed(1)}d`;
}

export default function InputProvenanceBadge({ intent }) {
  const [hover, setHover] = useState(false);
  const prov = intent?.evidence?.input_provenance;
  const trust = prov?.trust || "unknown";
  const tone = TONE[trust] || TONE.unknown;

  return (
    <span
      className="relative inline-flex items-center gap-1 cursor-help"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      data-testid={`intent-provenance-${intent?.intent_id || "unknown"}`}
      data-trust={trust}
    >
      <span
        className={`inline-block w-2 h-2 rounded-full ${tone.dot}`}
        aria-hidden="true"
      />
      <span className={`text-[10px] font-mono uppercase tracking-wider ${tone.text}`}>
        {tone.label}
      </span>
      {hover && (
        <span
          className="absolute z-50 left-0 top-5 bg-slate-950 border border-slate-700 rounded p-2 text-[10px] font-mono whitespace-pre text-slate-200 shadow-xl min-w-[180px]"
          data-testid={`intent-provenance-tooltip-${intent?.intent_id || "unknown"}`}
        >
          {prov ? (
            <>
              {`snapshot age: ${fmtAge(prov.snapshot_age_sec_at_emit)}\n`}
              {`bars:         ${prov.bars_seen_at_emit ?? "—"}\n`}
              {`source:       ${prov.snapshot_source_at_emit || "—"}\n`}
              {`tf:           ${prov.snapshot_tf_at_emit || "—"}\n`}
              {`computed:     ${prov.snapshot_computed_at || "—"}\n`}
              {`trust:        ${trust}`}
            </>
          ) : (
            "legacy intent — no provenance stamped"
          )}
        </span>
      )}
    </span>
  );
}
