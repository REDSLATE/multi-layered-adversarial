import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

/**
 * Live "active broker" pill for a lane (equity or crypto).
 *
 * Reads `/admin/broker-selection` (the same singleton the hamburger
 * BrokerSelectionMenu writes) and renders a small badge showing which
 * broker is currently being stamped on emitted intents for THIS lane.
 *
 * Subscribes to the `risedual:broker-selection-changed` window event
 * so it updates the instant the hamburger saves — no polling lag.
 * A 30s background refresh is also wired as belt-and-suspenders in
 * case the event misses (e.g. the operator changed broker selection
 * from a different tab / device).
 */
const META = {
  public: { label: "Public.com", tone: "default" },
  kraken: { label: "Kraken Pro", tone: "default" },
  webull: { label: "Webull",     tone: "override" },
};

export default function LaneRoutingPill({ lane }) {
  const [sel, setSel] = useState(null);
  const [defaults, setDefaults] = useState(null);

  const load = useCallback(async () => {
    try {
      const res = await api.get("/admin/broker-selection");
      setSel(res.data?.selection || null);
      setDefaults(res.data?.defaults || null);
    } catch {
      /* keep last known state */
    }
  }, []);

  useEffect(() => {
    let alive = true;
    const tick = () => { if (alive) load(); };
    tick();
    const onChanged = () => tick();
    window.addEventListener("risedual:broker-selection-changed", onChanged);
    const poll = setInterval(tick, 30_000);
    return () => {
      alive = false;
      window.removeEventListener("risedual:broker-selection-changed", onChanged);
      clearInterval(poll);
    };
  }, [load]);

  if (!sel) return null;
  const broker = sel[lane];
  const isDefault = defaults && sel[lane] === defaults[lane];
  const meta = META[broker] || { label: broker, tone: "default" };
  const tone = isDefault ? "default" : meta.tone;

  // Two-tone styling: default (zinc) vs. override (amber). Override
  // is the operator-meaningful state — Webull-for-crypto means Kraken
  // is being bypassed at brain-emit time.
  const cls = tone === "override"
    ? "border-amber-500/40 bg-amber-500/10 text-amber-300"
    : "border-zinc-500/30 bg-zinc-500/10 text-zinc-300";

  return (
    <span
      data-testid={`lane-routing-pill-${lane}`}
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 text-[10px] font-mono uppercase tracking-widest border ${cls} rounded-sm`}
      title={
        isDefault
          ? `Brain stamps ${meta.label} on emitted ${lane} intents (lane default).`
          : `Operator override active — brain stamps ${meta.label} on emitted ${lane} intents instead of the lane default.`
      }
    >
      <span className="opacity-60">routing</span>
      <span className="font-semibold">{meta.label}</span>
      {!isDefault && <span className="opacity-70">override</span>}
    </span>
  );
}
