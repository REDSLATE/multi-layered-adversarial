import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";

/**
 * LivePulse — small connection indicator for /runtime/{brain}.
 *
 * Reads `/api/heartbeat-status/{brain}` every 5s and renders:
 *   never  — grey dot, "no heartbeat yet"
 *   fresh  — green pulse, "connected · 21s ago"
 *   stale  — amber, "stale · 4m ago"
 *   dead   — red, "no heartbeat · 12m ago"
 *
 * Designed to sit in the header of the runtime detail page so the
 * operator can see at a glance whether the brain is actually online.
 * No auth — heartbeat-status endpoint is public (banding only, no leak).
 */
const STATE_META = {
  never:  { color: "#71717A", label: "no heartbeat yet",   pulse: false },
  fresh:  { color: "#10B981", label: "connected",          pulse: true  },
  stale:  { color: "#FBBF24", label: "stale",              pulse: false },
  dead:   { color: "#DC2626", label: "no heartbeat",       pulse: false },
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
        const r = await api.get(`/heartbeat-status/${runtime}`);
        if (!alive) return;
        setState(r.data);
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

  return (
    <div
      data-testid={`live-pulse-${runtime}`}
      data-state={state.connected}
      className="inline-flex items-center gap-2 text-[10px] font-mono uppercase tracking-widest"
      title={state.last_seen ? `last seen ${state.last_seen}` : "never connected"}
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
