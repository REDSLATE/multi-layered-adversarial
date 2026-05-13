import React, { useEffect, useRef, useState } from "react";
import { useTier } from "../context/TierContext";
import { mc, fmtAgo } from "../lib/mc";
import { Activity, CheckCircle2, AlertTriangle, XCircle, Info } from "lucide-react";

const SEVERITY_META = {
  info: { cls: "border-zinc-800 bg-zinc-950 text-zinc-300", icon: Info, dot: "bg-zinc-500" },
  success: { cls: "border-emerald-500/30 bg-emerald-500/5 text-emerald-200", icon: CheckCircle2, dot: "bg-emerald-400" },
  warn: { cls: "border-amber-500/30 bg-amber-500/5 text-amber-200", icon: AlertTriangle, dot: "bg-amber-400" },
  error: { cls: "border-rose-500/30 bg-rose-500/5 text-rose-200", icon: XCircle, dot: "bg-rose-400" },
};

function EventCard({ ev }) {
  const m = SEVERITY_META[ev.severity] || SEVERITY_META.info;
  const Icon = m.icon;
  return (
    <div
      data-testid={`rd-activity-event-${ev.event_id}`}
      className={`rounded-lg border p-4 ${m.cls}`}
    >
      <div className="flex items-start gap-3">
        <div className="mt-0.5">
          <Icon size={14} strokeWidth={1.8} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate font-display text-[14px] text-white">{ev.title}</span>
            {ev.symbol && (
              <span className="rounded-sm border border-zinc-700 bg-zinc-900 px-1.5 py-0.5 font-mono text-[10px] tracking-[0.12em] text-zinc-300">
                {ev.symbol}
              </span>
            )}
          </div>
          {ev.detail && (
            <div className="mt-1 line-clamp-2 text-[12px] text-zinc-400">
              {ev.detail}
            </div>
          )}
          <div className="mt-2 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.16em] text-zinc-600">
            <span>{ev.type}</span>
            <span>·</span>
            <span>{fmtAgo(ev.timestamp)}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function AgentActivity() {
  const { tier } = useTier();
  const [events, setEvents] = useState([]);
  const [polledAt, setPolledAt] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const seenIds = useRef(new Set());

  // Initial load + 10s polling.
  useEffect(() => {
    let cancelled = false;
    let timer = null;
    seenIds.current = new Set();

    const fetchOnce = async (since) => {
      const r = await mc.agentActivity(tier, { since, limit: 40 });
      if (cancelled) return;
      if (!r.ok) {
        setError(r.detail);
        setLoading(false);
        return;
      }
      setError(null);
      setLoading(false);
      setPolledAt(r.data.polled_at);
      const fresh = (r.data.items || []).filter((e) => !seenIds.current.has(e.event_id));
      fresh.forEach((e) => seenIds.current.add(e.event_id));
      if (fresh.length) {
        setEvents((prev) => [...fresh, ...prev].slice(0, 80));
      }
    };

    fetchOnce(null);
    timer = setInterval(() => {
      const lastTs = events[0]?.timestamp || null;
      fetchOnce(lastTs);
    }, 10000);

    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tier]);

  return (
    <div className="space-y-8" data-testid="rd-activity-page">
      <div className="flex items-end justify-between">
        <div>
          <div className="flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.22em] text-zinc-500">
            <Activity size={11} strokeWidth={2} className="text-emerald-400" />
            Live activity feed
          </div>
          <h1 className="mt-2 font-display text-3xl tracking-tight text-white md:text-4xl">
            What the council is doing.
          </h1>
        </div>
        <div className="hidden items-center gap-2 md:flex" data-testid="rd-activity-pulse">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400 shadow-[0_0_8px_rgba(16,185,129,0.8)]" />
          <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500">
            Polling · 10s · {polledAt ? fmtAgo(polledAt) : "—"}
          </span>
        </div>
      </div>

      {loading && (
        <div data-testid="rd-activity-loading" className="rounded-lg border border-zinc-900 bg-zinc-950/60 p-12 text-center font-mono text-[11px] uppercase tracking-[0.2em] text-zinc-600">
          Tuning into the feed…
        </div>
      )}

      {error && (
        <div data-testid="rd-activity-error" className="rounded-lg border border-rose-900/40 bg-rose-950/30 p-6 text-[13px] text-rose-300">
          {error}
        </div>
      )}

      {!loading && !error && events.length === 0 && (
        <div data-testid="rd-activity-empty" className="rounded-lg border border-zinc-900 bg-zinc-950/60 p-12 text-center text-[13px] text-zinc-500">
          No activity in the last while. The council is quiet — for now.
        </div>
      )}

      {events.length > 0 && (
        <div className="space-y-3" data-testid="rd-activity-list">
          {events.map((ev) => (
            <EventCard key={ev.event_id} ev={ev} />
          ))}
        </div>
      )}
    </div>
  );
}
