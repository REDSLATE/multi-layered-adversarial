import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";
import { Button } from "@/components/ui/button";
import KrakenConnect from "@/components/KrakenConnect";
import {
  Plug, ArrowsClockwise, Pulse, ShieldCheck, Warning, Lightning,
} from "@phosphor-icons/react";
import { toast } from "sonner";

function relTime(iso) {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/**
 * Kraken broker tile — sibling to AlpacaConnect on the Intents page.
 *
 * Doctrine: gives the operator a single place on the Intents page to see
 * whether Kraken's execution path is actually live. The OHLCV feeder
 * tile (FeedersStrip on /admin/overview) shows that price bars are
 * flowing in; this tile shows that orders CAN flow out — i.e. that
 * `execution_enabled=true` AND the auth probe passed. Without this
 * tile the operator had no signal from the Intents page that Kraken
 * was actually wired for trading vs. just feeding prices.
 *
 * Backs onto:
 *   GET     /api/admin/kraken/status      (full singleton snapshot)
 *   POST    /api/admin/kraken/reprobe     (re-probe scopes + balance)
 *   POST    /api/admin/kraken/poll        (force one OHLC tick)
 *   POST    /api/admin/kraken/execution   (toggle execution_enabled)
 *   POST    /api/admin/kraken/test        (cheap auth health check)
 *
 * Re-uses `<KrakenConnect />` (the existing modal) for the connect /
 * disconnect / re-key flow so we don't fork the credential UX.
 */
export default function KrakenBrokerTile() {
  return (
    <BrokerTileErrorBoundary>
      <KrakenBrokerTileInner />
    </BrokerTileErrorBoundary>
  );
}

class BrokerTileErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { err: null };
  }
  static getDerivedStateFromError(err) {
    return { err };
  }
  componentDidCatch(err, info) {
    // Surface the failure for debugging without crashing the page.
     
    console.error("KrakenBrokerTile crashed:", err, info);
  }
  render() {
    if (this.state.err) {
      return (
        <Card className="p-3 mb-4 border border-rd-danger" testid="kraken-broker-tile-crashed">
          <div className="flex items-baseline gap-2">
            <Warning size={12} weight="bold" className="text-rd-danger" />
            <span className="label-eyebrow text-rd-danger">Broker · Kraken (tile error)</span>
          </div>
          <div className="mt-2 text-[10px] font-mono text-rd-dim leading-relaxed">
            The Kraken broker tile failed to render. The rest of the page is unaffected.
            Use the Kraken slot under Overview · Feeder slots to manage credentials.
            <span className="block mt-1 text-rd-danger">
              {String(this.state.err?.message || this.state.err || "render error")}
            </span>
          </div>
        </Card>
      );
    }
    return this.props.children;
  }
}

function KrakenBrokerTileInner() {
  const [status, setStatus] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/kraken/status");
      setStatus(data);
      setErr("");
    } catch (e) {
      if (e?.response?.status !== 404) {
        setErr(e?.response?.data?.detail || e.message);
      }
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 20000);
    return () => clearInterval(t);
  }, [refresh]);

  const action = async (label, fn) => {
    setBusy(true);
    try {
      const r = await fn();
      toast.success(label);
      setStatus(r?.data || status);
      await refresh();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  };

  const toggleExecution = async () => {
    // Open the existing manage dialog and ask the operator to flip exec
    // there — that screen now has copy/fill/mismatch helpers.
    toast.message(
      `Open "Manage Kraken" to ${status?.execution_enabled ? "disable" : "enable"} execution`,
      { description: "Click \"Manage Kraken\" above, then use Execution authority → Enable/Disable with the phrase helpers." },
    );
  };

  // ── Not yet connected: minimal stub with the connect button ──
  if (!status || !status.connected) {
    return (
      <Card className="p-4 mb-4" testid="kraken-broker-tile">
        <div className="flex items-start justify-between gap-3 flex-wrap">
          <div className="flex items-baseline gap-2">
            <Plug size={14} weight="bold" className="text-rd-text" />
            <span className="label-eyebrow">Broker · Kraken</span>
            <Badge color="#A1A1AA">UNCONFIGURED</Badge>
          </div>
          <KrakenConnect />
        </div>
        {err && (
          <div className="mt-2 text-[11px] font-mono text-rd-danger" data-testid="kraken-broker-error">
            {err}
          </div>
        )}
        <div className="mt-3 text-[10px] text-rd-dim font-mono leading-relaxed">
          Connect a Kraken Pro API key pair to enable crypto execution on REDEYE&apos;s seat.
          The same key drives the OHLCV feeder on Overview · Feeder slots.
        </div>
      </Card>
    );
  }

  // ── Connected: full broker tile ──
  const execEnabled = Boolean(status.execution_enabled);
  const pollerRunning = Boolean(status.poller_running);
  const lastTickTs = status.last_tick?.ts;
  const lastTickBars = status.last_tick?.bars_pushed;
  const lastTickErr = status.last_tick?.error;
  const balancePrev = status.balance_preview;
  // Defensive: handle scopes whose values are objects (some Kraken
  // probe paths return {ok: true, detail: "..."} instead of a plain
  // bool). Coerce to truthy-bool before counting.
  const scopes = status.scopes && typeof status.scopes === "object" ? status.scopes : {};
  const scopeKeys = Object.keys(scopes);
  const goodScopes = scopeKeys.filter((k) => {
    const v = scopes[k];
    if (v === true) return true;
    if (v && typeof v === "object" && v.ok === true) return true;
    return false;
  });

  return (
    <Card className="p-0 mb-4 overflow-hidden" testid="kraken-broker-tile">
      <div className="px-4 py-3 border-b border-rd-border flex items-baseline justify-between gap-3 flex-wrap">
        <div className="flex items-baseline gap-2">
          <ShieldCheck size={14} weight="bold" className="text-rd-text" />
          <span className="label-eyebrow">Broker · Kraken</span>
          <Badge color={pollerRunning ? "#22C55E" : "#F59E0B"}>
            {pollerRunning ? "CONNECTED" : "CONNECTED · POLLER IDLE"}
          </Badge>
          <Badge color={execEnabled ? "#F59E0B" : "#22C55E"}>
            {execEnabled ? "EXEC ENABLED" : "READ-ONLY"}
          </Badge>
        </div>
        <div className="flex items-baseline gap-2">
          <KrakenConnect />
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => action("Ping", () => api.post("/admin/kraken/test"))}
            disabled={busy}
            data-testid="kraken-broker-ping"
          >
            <Pulse size={10} weight="bold" className="mr-1" /> ping
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => action("Re-probe scopes", () => api.post("/admin/kraken/reprobe"))}
            disabled={busy}
            data-testid="kraken-broker-reprobe"
          >
            <ArrowsClockwise size={10} weight="bold" className="mr-1" /> reprobe
          </Button>
          <Button
            type="button"
            size="sm"
            variant={execEnabled ? "destructive" : "default"}
            onClick={toggleExecution}
            disabled={busy}
            data-testid="kraken-broker-toggle-execution"
          >
            <Lightning size={10} weight="bold" className="mr-1" />
            {execEnabled ? "disable exec" : "enable exec"}
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 divide-y md:divide-y-0 md:divide-x divide-rd-border">
        <Cell label="key preview" value={status.public_key_preview || "—"} testid="kraken-broker-key" />
        <Cell label="pairs" value={(status.pairs || []).slice(0, 4).join(", ") || "—"} testid="kraken-broker-pairs" />
        <Cell label="tf · interval" value={`${status.tf || "—"} · ${status.poll_interval_seconds || "?"}s`} testid="kraken-broker-cadence" />
        <Cell
          label="last poll"
          value={lastTickTs ? `${relTime(lastTickTs)} · ${lastTickBars ?? 0} bars` : "—"}
          testid="kraken-broker-last-poll"
        />
      </div>

      <div className="px-4 py-2.5 bg-rd-bg2 border-t border-rd-border text-[10px] text-rd-dim font-mono leading-relaxed flex items-baseline justify-between flex-wrap gap-2">
        <span data-testid="kraken-broker-scopes">
          <span className="uppercase tracking-widest">scopes ok</span>
          {" · "}
          <span className="text-rd-text">{goodScopes.length}/{scopeKeys.length || "?"}</span>
          {goodScopes.length > 0 && (
            <span className="text-rd-dim"> · {goodScopes.join(", ")}</span>
          )}
        </span>
        {balancePrev && typeof balancePrev === "object" && Object.keys(balancePrev).length > 0 && (
          <span data-testid="kraken-broker-balance">
            <span className="uppercase tracking-widest">balance</span>
            {Object.entries(balancePrev).map(([asset, qty]) => (
              <span key={asset} className="ml-2">
                <span className="text-rd-dim">{asset}</span>{" "}
                <span className="text-rd-text">{String(qty)}</span>
              </span>
            ))}
          </span>
        )}
        {balancePrev && typeof balancePrev === "string" && (
          <span data-testid="kraken-broker-balance">
            <span className="uppercase tracking-widest">balance</span>
            {" · "}
            <span className="text-rd-text">{balancePrev}</span>
          </span>
        )}
        {lastTickErr && (
          <span className="text-rd-danger" data-testid="kraken-broker-poll-error">
            <Warning size={9} weight="bold" className="inline mr-1" />
            last tick · {String(lastTickErr)}
          </span>
        )}
      </div>

      {!execEnabled && (
        <div className="px-4 py-2 bg-rd-bg2 border-t border-rd-border text-[10px] font-mono text-rd-warning flex items-baseline gap-2">
          <Warning size={11} weight="bold" />
          <span>
            Execution is currently <span className="font-bold">READ-ONLY</span>. Crypto intents from REDEYE will be blocked at the broker boundary until you flip execution. Use the &quot;enable exec&quot; button above — it requires typing an explicit confirmation phrase.
          </span>
        </div>
      )}
    </Card>
  );
}

function Cell({ label, value, testid }) {
  return (
    <div className="px-3 py-2.5" data-testid={testid}>
      <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-1">{label}</div>
      <div className="text-xs font-mono text-rd-text break-words">{value}</div>
    </div>
  );
}
