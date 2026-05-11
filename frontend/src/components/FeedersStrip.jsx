import React, { useEffect, useState, useCallback } from "react";
import { api, relTime } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";
import { Plug, Pulse, WarningCircle, Power, Copy } from "@phosphor-icons/react";
import { toast } from "sonner";

const FEEDER_META = {
  kraken_pro: {
    label: "KRAKEN PRO",
    short: "KRKN",
    color: "#7B5CFF",
    market: "Crypto",
    docsUrl: "/runtime_patch_kit/technicals/README.md",
  },
  thinkorswim: {
    label: "THINKORSWIM",
    short: "TOS",
    color: "#22C55E",
    market: "Equities / Futures",
    docsUrl: "/runtime_patch_kit/technicals/README.md",
  },
  manual: {
    label: "MANUAL",
    short: "MAN",
    color: "#A1A1AA",
    market: "Backfill / CSV",
    docsUrl: "/runtime_patch_kit/technicals/README.md",
  },
};

const STATUS_META = {
  live:         { label: "LIVE",         color: "#22C55E", icon: Pulse },
  fresh:        { label: "FRESH",        color: "#22C55E", icon: Pulse },
  stale:        { label: "STALE",        color: "#F59E0B", icon: WarningCircle },
  awaiting:     { label: "AWAITING FEED", color: "#FACC15", icon: Power },
  unconfigured: { label: "UNCONFIGURED", color: "#A1A1AA", icon: Power },
  unknown:      { label: "UNKNOWN",      color: "#A1A1AA", icon: WarningCircle },
};

/**
 * Per-feeder connection slots. Kraken Pro gets headline placement
 * because it's the active crypto feeder; ThinkOrSwim and Manual sit
 * alongside. Click a slot to see setup details.
 */
export default function FeedersStrip() {
  const [items, setItems] = useState([]);
  const [endpoint, setEndpoint] = useState("/api/ingest/ohlcv");
  const [expanded, setExpanded] = useState(null);
  const [err, setErr] = useState("");

  const refresh = useCallback(async () => {
    try {
      const { data } = await api.get("/shared/technical/feeders");
      setItems(data.items || []);
      setEndpoint(data.endpoint);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 30000);
    return () => clearInterval(id);
  }, [refresh]);

  // Sort: kraken_pro first, then thinkorswim, then manual.
  const order = { kraken_pro: 0, thinkorswim: 1, manual: 2 };
  const sorted = [...items].sort((a, b) =>
    (order[a.key] ?? 99) - (order[b.key] ?? 99),
  );

  return (
    <Card className="p-0 overflow-hidden mb-6" testid="feeders-strip">
      <div className="px-4 py-3 border-b border-rd-border flex items-baseline justify-between">
        <div className="flex items-baseline gap-3">
          <Plug size={14} weight="bold" className="text-rd-text" />
          <span className="label-eyebrow">Feeder slots</span>
          <Badge color="#A1A1AA">{sorted.length} configured</Badge>
        </div>
        <div className="text-[10px] text-rd-dim uppercase tracking-widest">
          OHLCV ingress · shared evidence
        </div>
      </div>

      {err && (
        <div className="border-b border-rd-danger text-rd-danger px-3 py-2 text-xs font-mono">
          {err}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 divide-y md:divide-y-0 md:divide-x divide-rd-border">
        {sorted.map((f) => (
          <FeederSlot
            key={f.key}
            feeder={f}
            isOpen={expanded === f.key}
            onToggle={() => setExpanded(expanded === f.key ? null : f.key)}
            endpoint={endpoint}
          />
        ))}
      </div>
    </Card>
  );
}

function FeederSlot({ feeder, isOpen, onToggle, endpoint }) {
  const meta = FEEDER_META[feeder.key] || {};
  const statusMeta = STATUS_META[feeder.status] || STATUS_META.unknown;
  const Icon = statusMeta.icon;

  return (
    <div data-testid={`feeder-slot-${feeder.key}`}>
      <button
        type="button"
        onClick={onToggle}
        className="w-full px-4 py-3 text-left hover:bg-rd-bg2 transition-colors"
      >
        <div className="flex items-baseline gap-2 mb-1.5">
          <Badge color={meta.color}>{meta.short}</Badge>
          <span className="font-mono text-xs text-rd-text">{meta.label}</span>
          <span className="text-[10px] text-rd-dim uppercase tracking-widest ml-auto">
            {meta.market}
          </span>
        </div>
        <div className="flex items-baseline gap-2 mb-1.5">
          <Icon size={11} weight="bold" style={{ color: statusMeta.color }} />
          <span
            className="text-[10px] uppercase tracking-widest font-mono"
            style={{ color: statusMeta.color }}
          >
            {statusMeta.label}
          </span>
        </div>
        <div className="text-[11px] font-mono text-rd-dim leading-relaxed">
          {feeder.bars_count > 0 ? (
            <>
              {feeder.bars_count} bars · {feeder.symbols_count} symbols
              <br />
              last bar {feeder.last_bar_ts ? relTime(feeder.last_bar_ts) : "—"}
            </>
          ) : feeder.configured ? (
            <>token configured · no bars yet</>
          ) : (
            <>token missing from .env</>
          )}
        </div>
        <div className="mt-2 text-[10px] text-rd-dim uppercase tracking-widest">
          {isOpen ? "▾ hide setup" : "▸ show setup"}
        </div>
      </button>

      {isOpen && (
        <div
          className="px-4 py-3 bg-rd-bg2 border-t border-rd-border space-y-2 text-[11px] font-mono"
          data-testid={`feeder-setup-${feeder.key}`}
        >
          <SetupLine label="Endpoint" value={`POST ${endpoint}`} />
          <SetupLine label="Auth header" value={`X-Feeder-Token: $${feeder.env_key}`} />
          <SetupLine label="source field" value={feeder.key} />
          {feeder.symbols.length > 0 && (
            <SetupLine label="symbols" value={feeder.symbols.join(", ")} />
          )}
          {feeder.tfs.length > 0 && (
            <SetupLine label="timeframes" value={feeder.tfs.join(", ")} />
          )}
          <div className="pt-2 mt-2 border-t border-rd-border text-[10px] text-rd-muted leading-relaxed">
            Drop the example sidecar from{" "}
            <span className="text-rd-text">/app/runtime_patch_kit/technicals/README.md</span>{" "}
            on a machine that can reach {feeder.key === "kraken_pro" ? "Kraken" : "your data source"}.
            Set <span className="text-rd-text">{feeder.env_key}</span> and{" "}
            <span className="text-rd-text">MC_URL</span> on that host. Polls run on a schedule;
            re-ingest of the same bar is idempotent.
          </div>
        </div>
      )}
    </div>
  );
}

function SetupLine({ label, value }) {
  const copy = () => {
    navigator.clipboard.writeText(value).then(() => {
      toast.success(`${label} copied`);
    }).catch(() => {});
  };
  return (
    <div className="flex items-baseline gap-3">
      <span className="text-[10px] text-rd-dim uppercase tracking-widest w-24 shrink-0">
        {label}
      </span>
      <code className="text-rd-text break-all flex-1">{value}</code>
      <button
        type="button"
        onClick={copy}
        className="text-rd-dim hover:text-rd-text shrink-0"
        title="copy"
      >
        <Copy size={11} weight="bold" />
      </button>
    </div>
  );
}
