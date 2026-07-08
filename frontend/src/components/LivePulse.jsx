import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";

/**
 * LivePulse — small connection indicator for /runtime/{brain}.
 *
 * Reads `/api/admin/runtime/{brain}/status` every 5s and renders:
 *   never  — grey dot, "no heartbeat yet"
 *   fresh  — green pulse, "connected · 21s ago"
 *   stale  — amber, "stale · 4m ago"
 *   dead   — red, "no heartbeat · 12m ago"
 *
 * Designed to sit in the header of the runtime detail page so the
 * operator can see at a glance whether the brain is actually online.
 *
 * NOTE (2026-06 consolidation): the old public `/api/heartbeat-status/
 * {brain}` route was retired in favor of the unified admin status
 * endpoint. That endpoint requires auth — the `api` client adds the
 * bearer automatically. Response shape is `{payload: {heartbeat: {...}}}`;
 * we derive age from `heartbeat.last_seen` client-side.
 */
const STATE_META = {
  never:     { color: "#71717A", label: "no heartbeat yet",   pulse: false },
  connected: { color: "#10B981", label: "connected",          pulse: true  },
  partial:   { color: "#FBBF24", label: "heartbeat only",     pulse: false },
  stale:     { color: "#F97316", label: "stale",              pulse: false },
  dead:      { color: "#DC2626", label: "no heartbeat",       pulse: false },
  // Back-compat: older endpoint used `fresh` as the green state.
  fresh:     { color: "#10B981", label: "connected",          pulse: true  },
};

function fmtAge(seconds) {
  if (seconds == null) return "";
  const s = Math.round(seconds);
  if (s < 90) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export default function LivePulse({ runtime }) {
  const [state, setState] = useState({ connected: "never", age_seconds: null });
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;

    async function poll() {
      try {
        const r = await api.get(`/admin/runtime/${runtime}/status`);
        if (!alive) return;
        const hb = (r.data?.payload?.heartbeat) || {};
        let hbAgeS = null;
        if (hb.last_seen) {
          const d = new Date(hb.last_seen);
          if (!Number.isNaN(d.getTime())) {
            hbAgeS = Math.max(0, (Date.now() - d.getTime()) / 1000);
          }
        }
        // Bands: <60s connected · <5m partial (heartbeat but stale
        // opinion cadence) · <15m stale · beyond = dead. `never` when
        // the heartbeat row simply doesn't exist yet.
        let connected;
        if (!hb.last_seen) {
          connected = "never";
        } else if (hbAgeS < 60) {
          connected = "connected";
        } else if (hbAgeS < 300) {
          connected = "partial";
        } else if (hbAgeS < 900) {
          connected = "stale";
        } else {
          connected = "dead";
        }
        const svAgeS =
          typeof hb.sovereign_age_s === "number" ? hb.sovereign_age_s : null;
        setState({
          connected,
          age_seconds: hbAgeS,
          heartbeat_age_seconds: hbAgeS,
          contribution_age_seconds: svAgeS,
          last_seen: hb.last_seen || null,
        });
      } catch {
        if (alive) setState({ connected: "never", age_seconds: null });
      } finally {
        if (alive) setLoaded(true);
      }
    }

    poll();
    const t = setInterval(poll, 5000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [runtime]);

  const meta = STATE_META[state.connected] || STATE_META.never;
  const age = fmtAge(state.age_seconds);

  // Human-readable diagnostic for the hover tooltip — surfaces WHY a
  // brain is in `partial` / `stale` state so the operator doesn't have
  // to dig through the API.
  const hbAge = fmtAge(state.heartbeat_age_seconds);
  const svAge = fmtAge(state.contribution_age_seconds);
  const tip = [
    state.last_seen ? `last seen ${state.last_seen}` : "never connected",
    `heartbeat: ${hbAge || "never"}`,
    `contribution: ${svAge || "never"}`,
  ].join(" · ");

  return (
    <div
      data-testid={`live-pulse-${runtime}`}
      data-state={state.connected}
      className="inline-flex items-center gap-2 text-[10px] font-mono uppercase tracking-widest"
      title={tip}
    >
      <span className="relative inline-flex items-center justify-center w-2.5 h-2.5">
        {meta.pulse && (
          <span
            className="absolute inset-0 rounded-full animate-ping opacity-75"
            style={{ backgroundColor: meta.color }}
          />
        )}
        <span
          className="relative rounded-full w-2.5 h-2.5"
          style={{ backgroundColor: meta.color }}
        />
      </span>
      <span style={{ color: meta.color }}>
        {meta.label}
        {age ? <span className="text-rd-dim ml-1">· {age}</span> : null}
      </span>
      {!loaded && <span className="text-rd-dim">(loading)</span>}
    </div>
  );
}
