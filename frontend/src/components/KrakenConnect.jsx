import React, { useEffect, useState, useCallback } from "react";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { KeyholeIcon, ShieldCheck, Warning, Plug, Trash, ArrowsClockwise, Lightning } from "@phosphor-icons/react";
import { toast } from "sonner";

const PAIR_OPTIONS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD", "DOGE/USD"];
const TF_OPTIONS = ["5m", "15m", "1h", "4h", "1d"];

/**
 * Kraken Pro connection panel — credential modal + status. Renders inside
 * the Kraken slot on the Mission page (FeedersStrip) when the operator
 * clicks "Connect Kraken Pro" or "Manage". Doctrine: never displays the
 * private key after save; shows redacted preview + scope flags.
 */
export default function KrakenConnect({ trigger = "auto", onChange }) {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState(null);
  const [loadingStatus, setLoadingStatus] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/kraken/status");
      setStatus(data);
      onChange?.(data);
    } catch (e) {
      // 404 is normal when nothing is configured
      if (e?.response?.status !== 404) {
        toast.error(e?.response?.data?.detail || e.message);
      }
    } finally {
      setLoadingStatus(false);
    }
  }, [onChange]);

  useEffect(() => { refresh(); }, [refresh]);

  const connected = status?.connected;

  return (
    <>
      <div className="flex items-baseline gap-2" data-testid="kraken-connect-block">
        <Button
          size="sm"
          variant={connected ? "secondary" : "default"}
          onClick={() => setOpen(true)}
          data-testid="kraken-connect-trigger"
        >
          {connected ? "Manage Kraken" : "Connect Kraken Pro"}
        </Button>
        {connected && (
          <Badge color={status.execution_enabled ? "#F59E0B" : "#22C55E"}>
            {status.execution_enabled ? "EXEC ENABLED" : "READ-ONLY"}
          </Badge>
        )}
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="bg-rd-bg2 border-rd-border max-w-2xl">
          <DialogHeader>
            <DialogTitle className="flex items-baseline gap-2">
              <KeyholeIcon size={16} weight="bold" />
              {connected ? "Kraken Pro · connected" : "Connect Kraken Pro"}
            </DialogTitle>
            <DialogDescription className="text-rd-dim text-[11px] font-mono">
              Keys stored encrypted at rest. Private key never returned.
              Default permission scope is READ-ONLY — execution stays
              schema-pinned off until you explicitly authorize it.
            </DialogDescription>
          </DialogHeader>

          {!loadingStatus && (
            connected
              ? <ConnectedView status={status} onChange={refresh} onClose={() => setOpen(false)} />
              : <ConnectForm onSaved={() => { refresh(); setOpen(false); }} />
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}

function ConnectForm({ onSaved }) {
  const [apiKey, setApiKey] = useState("");
  const [privateKey, setPrivateKey] = useState("");
  const [pairs, setPairs] = useState(["BTC/USD", "ETH/USD"]);
  const [tf, setTf] = useState("1h");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState("");

  const togglePair = (p) => {
    setPairs(pairs.includes(p) ? pairs.filter(x => x !== p) : [...pairs, p]);
  };

  const submit = async () => {
    setErr("");
    if (!apiKey.trim() || !privateKey.trim()) {
      setErr("both keys required");
      return;
    }
    if (pairs.length === 0) {
      setErr("select at least one pair");
      return;
    }
    setSubmitting(true);
    try {
      await api.post("/admin/kraken/connect", {
        api_key: apiKey.trim(),
        private_key: privateKey.trim(),
        pairs, tf,
      });
      toast.success("Kraken connected — auto-poller running");
      setApiKey(""); setPrivateKey("");
      onSaved?.();
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-4 text-sm">
      <div className="border border-rd-warning/40 bg-rd-warning/5 px-3 py-2 text-[11px] font-mono text-rd-warning flex gap-2">
        <Warning size={14} weight="bold" />
        <div>
          Create a Kraken API key scoped READ-ONLY: tick "Query Funds",
          "Query Open Orders & Trades", "Query Closed Orders & Trades",
          "Query Ledger Entries". Leave Trade / Withdraw <span className="font-bold">unchecked</span>.
        </div>
      </div>

      <div>
        <Label htmlFor="kraken-api-key" className="text-[10px] uppercase tracking-widest text-rd-dim">
          API Key
        </Label>
        <Input
          id="kraken-api-key"
          data-testid="kraken-api-key-input"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          placeholder="public key from Kraken Settings → API"
          className="font-mono text-xs bg-rd-bg3 border-rd-border"
        />
      </div>

      <div>
        <Label htmlFor="kraken-private-key" className="text-[10px] uppercase tracking-widest text-rd-dim">
          Private Key (base64)
        </Label>
        <Input
          id="kraken-private-key"
          data-testid="kraken-private-key-input"
          type="password"
          value={privateKey}
          onChange={(e) => setPrivateKey(e.target.value)}
          autoComplete="new-password"
          spellCheck={false}
          placeholder="paste once — never shown again"
          className="font-mono text-xs bg-rd-bg3 border-rd-border"
        />
      </div>

      <div>
        <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-1.5">
          Pairs to auto-pull
        </div>
        <div className="flex flex-wrap gap-1.5">
          {PAIR_OPTIONS.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => togglePair(p)}
              className={`px-2 py-1 text-[11px] font-mono border ${
                pairs.includes(p)
                  ? "border-rd-text text-rd-text bg-rd-bg3"
                  : "border-rd-border text-rd-dim hover:text-rd-text"
              }`}
              data-testid={`kraken-pair-${p.replace("/", "-")}`}
            >
              {p}
            </button>
          ))}
        </div>
      </div>

      <div>
        <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-1.5">Timeframe</div>
        <div className="flex gap-1.5">
          {TF_OPTIONS.map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setTf(t)}
              className={`px-2 py-1 text-[11px] font-mono border ${
                tf === t
                  ? "border-rd-text text-rd-text bg-rd-bg3"
                  : "border-rd-border text-rd-dim hover:text-rd-text"
              }`}
            >
              {t}
            </button>
          ))}
        </div>
      </div>

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 text-[11px] font-mono">
          {err}
        </div>
      )}

      <DialogFooter className="flex items-center justify-between gap-2">
        <span className="text-[10px] text-rd-dim font-mono">
          On save we probe Balance to confirm the keys are alive, then start the poller.
        </span>
        <Button
          onClick={submit}
          disabled={submitting}
          data-testid="kraken-save-btn"
          className="bg-rd-text text-rd-bg hover:bg-rd-muted"
        >
          {submitting ? "TESTING + SAVING…" : "TEST & CONNECT"}
        </Button>
      </DialogFooter>
    </div>
  );
}

function ConnectedView({ status, onChange, onClose }) {
  const [busy, setBusy] = useState("");
  const [showExecToggle, setShowExecToggle] = useState(false);

  const action = async (label, fn) => {
    setBusy(label);
    try {
      await fn();
      toast.success(`${label} OK`);
      await onChange();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="space-y-3 text-sm">
      {/* Key preview */}
      <div className="grid grid-cols-2 gap-3 text-[11px] font-mono">
        <KV label="API Key" value={status.public_key_preview} />
        <KV label="Private Key" value={status.private_key_preview} />
        <KV label="Connected by" value={status.connected_by} />
        <KV label="Updated" value={status.updated_at ? new Date(status.updated_at).toLocaleString() : "—"} />
      </div>

      {/* Scopes */}
      <Card className="p-3">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2 flex items-baseline gap-2">
          <ShieldCheck size={11} weight="bold" />
          Detected permissions
        </div>
        <div className="grid grid-cols-2 gap-1.5 text-[11px] font-mono" data-testid="kraken-scopes">
          {Object.entries(status.scopes || {}).map(([scope, ok]) => (
            <div key={scope} className="flex items-baseline gap-2">
              <Badge color={ok ? "#22C55E" : "#A1A1AA"}>{ok ? "✓" : "✗"}</Badge>
              <span className="text-rd-text">{scope}</span>
            </div>
          ))}
        </div>
        {status.balance_preview && Object.keys(status.balance_preview).length > 0 && (
          <div className="mt-2 pt-2 border-t border-rd-border text-[11px] font-mono">
            <span className="text-[10px] uppercase tracking-widest text-rd-dim">Balance preview · </span>
            {Object.entries(status.balance_preview).map(([asset, qty]) => (
              <span key={asset} className="ml-2">
                <span className="text-rd-dim">{asset}</span> <span className="text-rd-text">{qty}</span>
              </span>
            ))}
          </div>
        )}
      </Card>

      {/* Pairs + poller */}
      <Card className="p-3 text-[11px] font-mono">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2 flex items-baseline gap-2">
          <ArrowsClockwise size={11} weight="bold" />
          Auto-pull
        </div>
        <div>Pairs: <span className="text-rd-text">{(status.pairs || []).join(", ")}</span></div>
        <div>Timeframe: <span className="text-rd-text">{status.tf}</span> · every {status.poll_interval_seconds}s</div>
        <div>
          Poller: <Badge color={status.poller_running ? "#22C55E" : "#A1A1AA"}>
            {status.poller_running ? "RUNNING" : "IDLE"}
          </Badge>
          {status.last_tick?.ts && (
            <span className="ml-2 text-rd-dim">
              last tick {new Date(status.last_tick.ts).toLocaleTimeString()} ·
              pushed {status.last_tick.bars_pushed} bars
            </span>
          )}
        </div>
        {status.last_tick?.error && (
          <div className="text-rd-danger mt-1">tick error: {status.last_tick.error}</div>
        )}
      </Card>

      {/* Execution gate */}
      <Card className="p-3">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2 flex items-baseline gap-2">
          <Lightning size={11} weight="bold" />
          Execution authority
        </div>
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-[11px] font-mono">
            {status.execution_enabled ? (
              <span className="text-rd-warning">ENABLED — trade endpoints unlocked for future use</span>
            ) : (
              <span className="text-rd-text">DISABLED · doctrine default</span>
            )}
          </span>
          <Button
            size="sm"
            variant="outline"
            onClick={() => setShowExecToggle(!showExecToggle)}
            data-testid="kraken-exec-toggle-btn"
          >
            {showExecToggle ? "cancel" : (status.execution_enabled ? "Disable…" : "Enable…")}
          </Button>
        </div>
        {showExecToggle && (
          <ExecutionToggle
            currentlyEnabled={status.execution_enabled}
            onDone={() => { setShowExecToggle(false); onChange(); }}
          />
        )}
      </Card>

      <DialogFooter className="flex items-center justify-between gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={() => action("Re-probe", () => api.post("/admin/kraken/reprobe"))}
          disabled={busy !== ""}
          data-testid="kraken-reprobe-btn"
        >
          {busy === "Re-probe" ? "…" : "Re-probe scopes"}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => action("Force poll", () => api.post("/admin/kraken/poll"))}
          disabled={busy !== ""}
          data-testid="kraken-force-poll-btn"
        >
          {busy === "Force poll" ? "…" : "Force OHLC poll"}
        </Button>
        <Button
          variant="destructive"
          size="sm"
          onClick={async () => {
            if (!confirm("Disconnect Kraken? Stored keys will be deleted.")) return;
            await action("Disconnect", () => api.delete("/admin/kraken/disconnect"));
            onClose?.();
          }}
          disabled={busy !== ""}
          data-testid="kraken-disconnect-btn"
        >
          <Trash size={12} weight="bold" className="mr-1" /> Disconnect
        </Button>
      </DialogFooter>
    </div>
  );
}

function ExecutionToggle({ currentlyEnabled, onDone }) {
  const newState = !currentlyEnabled;
  const expectedPhrase = newState ? "I authorize execution on Kraken" : "Disable execution";
  const [phrase, setPhrase] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    setSubmitting(true);
    try {
      await api.post("/admin/kraken/execution", {
        enabled: newState,
        confirm: phrase,
      });
      toast.success(`Execution ${newState ? "ENABLED" : "DISABLED"}`);
      onDone();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="mt-3 border-t border-rd-border pt-3 space-y-2">
      <div className="text-[11px] font-mono text-rd-dim">
        Type the literal phrase below to confirm flipping execution {newState ? "ON" : "OFF"}:
      </div>
      <code className="block text-[11px] font-mono text-rd-text bg-rd-bg3 px-2 py-1 border border-rd-border">
        {expectedPhrase}
      </code>
      <Input
        value={phrase}
        onChange={(e) => setPhrase(e.target.value)}
        autoComplete="off"
        spellCheck={false}
        className="font-mono text-xs bg-rd-bg3 border-rd-border"
        data-testid="kraken-exec-confirm-input"
      />
      <Button
        size="sm"
        onClick={submit}
        disabled={submitting || phrase !== expectedPhrase}
        data-testid="kraken-exec-confirm-btn"
        variant={newState ? "destructive" : "default"}
      >
        {submitting ? "…" : (newState ? "ENABLE EXECUTION" : "DISABLE EXECUTION")}
      </Button>
    </div>
  );
}

function KV({ label, value }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-rd-dim">{label}</div>
      <div className="text-rd-text">{value || "—"}</div>
    </div>
  );
}
