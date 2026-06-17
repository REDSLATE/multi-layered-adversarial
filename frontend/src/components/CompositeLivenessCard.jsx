import React, { useCallback, useEffect, useState } from "react";
import { api, RUNTIME_META } from "@/lib/api";
import { Card } from "@/components/ui-bits";

// Per-loop liveness card — surfaces MC's `composite_liveness` block
// from /api/admin/brain/emission-diagnose/{brain} for every brain on
// one page. Built 2026-02-20 after operator caught the REDEYE pattern:
// "DEAD by heartbeat, but actively passing gate checks 45s ago" — the
// old badge was hiding the fact that one loop in the brain can die
// while others stay healthy.
//
// What this renders per brain:
//   - Overall band (LIVE / LIVE_DEGRADED / LIVE_IDLE / STALE / DEAD / NEVER)
//   - Reason chips (STALE_HEARTBEAT, ENGINE_ACTIVE, etc.)
//   - Per-loop bands for all 6 loops with ages
//
// Read-only. Does not affect routing/authority. Refreshes every 10s.

const BRAINS = ["camino", "barracuda", "hellcat", "gto"];

const OVERALL_COLORS = {
  LIVE: "#10B981",
  LIVE_DEGRADED: "#F59E0B",
  LIVE_IDLE: "#3B82F6",
  STALE: "#F59E0B",
  DEAD: "#DC2626",
  NEVER: "#A1A1AA",
};

const BAND_COLORS = {
  live: "#10B981",
  stale: "#F59E0B",
  dead: "#DC2626",
  never: "#A1A1AA",
};

const CHIP_COLORS = {
  LIVE: "#10B981",
  LIVE_DEGRADED: "#F59E0B",
  LIVE_IDLE: "#3B82F6",
  STALE: "#F59E0B",
  DEAD: "#DC2626",
  NEVER: "#A1A1AA",
  STALE_HEARTBEAT: "#F59E0B",
  DEAD_HEARTBEAT: "#DC2626",
  STALE_SOVEREIGN: "#F59E0B",
  STALE_OPINION: "#F59E0B",
  ENGINE_ACTIVE: "#10B981",
};

const LOOP_LABEL = {
  heartbeat_loop: "heartbeat",
  checkin_loop: "checkin",
  engine_loop: "engine",
  directional_loop: "directional",
  sovereign_loop: "sovereign",
  opinion_loop: "opinion",
};

function fmtAge(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function LoopRow({ loopKey, loop }) {
  const color = BAND_COLORS[loop.band] || "#A1A1AA";
  const age = loop.age_seconds;
  const count = loop.count_last_1h;
  const detail =
    age != null
      ? fmtAge(age)
      : count != null
      ? `${count}/1h`
      : "—";
  return (
    <div
      className="flex items-center justify-between text-[11px] font-mono py-0.5"
      data-testid={`composite-loop-${loopKey}`}
    >
      <span className="text-rd-dim">{LOOP_LABEL[loopKey] || loopKey}</span>
      <span className="flex items-center gap-2">
        <span style={{ color, fontWeight: 600 }}>
          {loop.band.toUpperCase()}
        </span>
        <span className="text-rd-text">{detail}</span>
      </span>
    </div>
  );
}

function BrainColumn({ brain, data }) {
  const meta = RUNTIME_META[brain];
  const cl = data?.composite_liveness;
  if (!cl) {
    return (
      <div
        className="border border-rd-border p-3 bg-rd-bg2"
        data-testid={`composite-card-${brain}`}
      >
        <div className="flex items-center justify-between mb-2">
          <span style={{ color: meta.color }} className="font-bold text-xs">
            {meta.label}
          </span>
          <span className="text-[10px] text-rd-dim font-mono">no data</span>
        </div>
      </div>
    );
  }
  const overallColor = OVERALL_COLORS[cl.overall] || "#A1A1AA";
  const chips = (cl.chips || []).filter((c) => c !== cl.overall);
  return (
    <div
      className="border border-rd-border p-3 bg-rd-bg2"
      data-testid={`composite-card-${brain}`}
    >
      <div className="flex items-center justify-between mb-2">
        <span style={{ color: meta.color }} className="font-bold text-xs">
          {meta.label}
        </span>
        <span
          className="font-display font-bold text-sm"
          style={{ color: overallColor }}
          data-testid={`composite-overall-${brain}`}
        >
          {cl.overall}
        </span>
      </div>

      {chips.length > 0 && (
        <div
          className="flex flex-wrap gap-1 mb-3"
          data-testid={`composite-chips-${brain}`}
        >
          {chips.map((chip) => (
            <span
              key={chip}
              className="text-[9px] font-mono px-1.5 py-0.5 border"
              style={{
                color: CHIP_COLORS[chip] || "#A1A1AA",
                borderColor: CHIP_COLORS[chip] || "#A1A1AA",
              }}
            >
              {chip}
            </span>
          ))}
        </div>
      )}

      <div className="space-y-0.5">
        {Object.entries(cl.loops || {}).map(([k, v]) => (
          <LoopRow key={k} loopKey={k} loop={v} />
        ))}
      </div>
    </div>
  );
}

export default function CompositeLivenessCard() {
  const [byBrain, setByBrain] = useState({});
  const [err, setErr] = useState("");
  // 2026-02-19 (prod incident): track when each brain's data was
  // last refreshed successfully so transient fetch failures don't
  // wipe the card to "no data" everywhere. The previous behaviour
  // (overwrite byBrain on every poll, including with nulls on
  // failure) caused the cyclic blank-card flicker the operator
  // observed on mobile during Webull-SDK overload windows.
  const [lastRefreshAt, setLastRefreshAt] = useState({});
  const [staleCount, setStaleCount] = useState(0);

  const load = useCallback(async () => {
    let anyOk = false;
    const updates = {};
    const updatedTs = {};
    const now = new Date();
    await Promise.all(
      BRAINS.map(async (b) => {
        try {
          const { data } = await api.get(
            `/admin/brain/emission-diagnose/${b}`,
          );
          updates[b] = data;
          updatedTs[b] = now;
          anyOk = true;
        } catch {
          // Per-brain failure — leave the brain's previous value
          // in place so the card stays populated.
        }
      }),
    );
    setByBrain((prev) => {
      const next = { ...prev };
      for (const [b, d] of Object.entries(updates)) next[b] = d;
      return next;
    });
    setLastRefreshAt((prev) => ({ ...prev, ...updatedTs }));
    if (anyOk) {
      setErr("");
      setStaleCount(0);
    } else {
      setErr("All brains failed to refresh — showing stale data.");
      setStaleCount((n) => n + 1);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <Card testid="composite-liveness-card" className="mb-6">
      <div className="flex items-baseline justify-between mb-3">
        <div>
          <div className="label-eyebrow">Per-loop liveness</div>
          <div className="font-display text-lg font-bold tracking-tight">
            Composite brain status
          </div>
        </div>
        <div className="text-[10px] text-rd-dim font-mono">
          one badge was lying · this view shows every loop · refreshes 10s
        </div>
      </div>

      {err && staleCount >= 2 && (
        <div className="border border-rd-warn text-rd-warn px-3 py-1.5 mb-3 text-[11px] font-mono" data-testid="composite-liveness-stale">
          {err} · {staleCount} consecutive refresh failures
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
        {BRAINS.map((b) => (
          <BrainColumn key={b} brain={b} data={byBrain[b]} />
        ))}
      </div>
    </Card>
  );
}
