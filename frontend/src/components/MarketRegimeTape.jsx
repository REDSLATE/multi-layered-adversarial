import React, { useEffect, useState, useMemo } from "react";
import { api, relTime } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";
import { useMcStream } from "@/hooks/useMcStream";

/**
 * Doctrine pin (2026-06-10, P2):
 *
 * Before this card, `market_regime` was hardcoded "calm" everywhere
 * AND invisible to the operator. With the regime detector landed
 * (`shared/market_regime.py`) AND the SSE stream pushing every
 * regime CHANGE, this card shows the running tape of the last N
 * regime classifications so operators can spot transitions in
 * real time. Live updates via SSE — no polling.
 */

const REGIME_META = {
  bull:     { color: "#10B981", label: "BULL",     blurb: "trend + breadth aligned up" },
  bear:     { color: "#EF4444", label: "BEAR",     blurb: "trend + breadth aligned down" },
  chop:     { color: "#F59E0B", label: "CHOP",     blurb: "symmetric noise, no commitment" },
  volatile: { color: "#EAB308", label: "VOLATILE", blurb: "elevated realized vol" },
  crisis:   { color: "#DC2626", label: "CRISIS",   blurb: "extreme vol — risk off everything" },
  calm:     { color: "#A1A1AA", label: "CALM",     blurb: "mixed signals, low energy" },
};

function regimeColor(regime) {
  return REGIME_META[regime]?.color || "#52525B";
}

function RegimePill({ regime, ts, size = "md" }) {
  const meta = REGIME_META[regime] || { color: "#52525B", label: regime?.toUpperCase() || "—", blurb: "" };
  const px = size === "lg" ? "px-4 py-2 text-base" : "px-3 py-1 text-sm";
  return (
    <span
      className={`inline-flex items-center gap-2 rounded-full font-medium ${px}`}
      style={{
        background: `${meta.color}22`,
        color: meta.color,
        border: `1px solid ${meta.color}66`,
      }}
      title={ts ? `${meta.blurb} · ${relTime(ts)}` : meta.blurb}
      data-testid={`regime-pill-${regime}`}
    >
      <span
        className="h-2 w-2 rounded-full"
        style={{ background: meta.color }}
      />
      {meta.label}
    </span>
  );
}

export default function MarketRegimeTape() {
  const { byType, currentRegime, connected } = useMcStream({ cap: 60 });
  const [seed, setSeed] = useState(null);
  const [seedErr, setSeedErr] = useState(null);

  // Seed: pull the most recent intent so we know the regime BEFORE
  // the first SSE regime event lands (which only fires on change).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await api.get("/admin/runtime/camaro/intent-summary?minutes=15&limit=1");
        if (cancelled) return;
        const recent = r.data?.recent?.[0];
        if (recent) {
          setSeed({
            regime: recent?.snapshot?.market_regime || null,
            ts: recent?.ingest_ts,
          });
        }
      } catch (e) {
        // Soft-fail; tape will populate from SSE within ~2s.
        setSeedErr(e?.response?.data?.detail || e.message);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const tape = useMemo(() => {
    // Build a deduped chronological tape:
    // newest regime first, only keep transitions (consecutive
    // duplicates collapse to a single entry).
    const events = (byType.regime || []).slice();
    if (seed?.regime && events.length === 0) {
      events.push({ regime: seed.regime, ts: seed.ts });
    }
    const dedup = [];
    for (const e of events) {
      if (dedup.length === 0 || dedup[dedup.length - 1].regime !== e.regime) {
        dedup.push(e);
      }
    }
    return dedup;
  }, [byType.regime, seed]);

  const current = currentRegime || seed?.regime || null;
  const meta = current ? REGIME_META[current] : null;

  return (
    <Card testid="market-regime-tape-card">
      <div className="flex items-center justify-between mb-3">
        <div>
          <div className="text-xs uppercase tracking-wider text-zinc-500">
            Market Regime · live tape
          </div>
          <div className="text-sm text-zinc-400 mt-1">
            Composite of mean trend, realized vol, and breadth across the universe.
            Emitted every tick from `_rank_universe`.
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span
            className="h-2 w-2 rounded-full"
            style={{ background: connected ? "#10B981" : "#71717A" }}
            data-testid="regime-tape-conn-dot"
          />
          <span className="text-xs text-zinc-500">
            {connected ? "live" : "reconnecting"}
          </span>
        </div>
      </div>

      <div className="flex items-center gap-3 mb-3" data-testid="regime-tape-current">
        {current ? (
          <>
            <RegimePill regime={current} size="lg" />
            <span className="text-xs text-zinc-500 italic">{meta?.blurb}</span>
          </>
        ) : (
          <span className="text-sm text-zinc-500">
            {seedErr ? `seed failed: ${seedErr}` : "awaiting first regime classification…"}
          </span>
        )}
      </div>

      <div className="border-t border-zinc-800 pt-3">
        <div className="text-xs uppercase tracking-wider text-zinc-500 mb-2">
          Recent transitions
        </div>
        {tape.length === 0 ? (
          <div className="text-sm text-zinc-500">
            No transitions since connect. The regime stays put until the universe scan disagrees.
          </div>
        ) : (
          <div
            className="flex flex-wrap gap-2 items-center"
            data-testid="regime-tape-transitions"
          >
            {tape.slice(0, 12).map((e, i) => (
              <div
                key={`${e.ts}-${i}`}
                className="flex items-center gap-2"
              >
                <RegimePill regime={e.regime} ts={e.ts} />
                <span className="text-xs text-zinc-600">{relTime(e.ts)}</span>
                {i < tape.length - 1 && i < 11 && (
                  <span className="text-zinc-700">·</span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </Card>
  );
}

export { RegimePill, regimeColor, REGIME_META };
