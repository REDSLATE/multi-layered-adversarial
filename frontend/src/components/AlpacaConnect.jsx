import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { KeyholeIcon, Plug, ArrowsClockwise, Pulse, Trash, ShieldCheck } from "@phosphor-icons/react";
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

function fmtUSD(v) {
  if (v == null) return "—";
  const n = Number(v);
  if (isNaN(n)) return "—";
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/**
 * Alpaca paper-trading connection tile. Mounts inside the Intents page
 * directly under the Executor seat — same "operator-only credentials"
 * pattern as KrakenConnect. Keys are Fernet-encrypted server-side; UI
 * only ever shows redacted previews after save.
 */
export default function AlpacaConnect() {
  const [status, setStatus] = useState(null);
  const [caps, setCaps] = useState(null);
  const [open, setOpen] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [s, c] = await Promise.all([
        api.get("/admin/alpaca/status"),
        api.get("/execution/caps"),
      ]);
      setStatus(s.data);
      setCaps(c.data);
    } catch (e) {
      // 401 surfaces elsewhere; ignore
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 20000);
    return () => clearInterval(t);
  }, [refresh]);

  const connect = async () => {
    setBusy(true);
    setErr("");
    try {
      const res = await api.post("/admin/alpaca/connect", {
        api_key_id: apiKey.trim(),
        secret_key: secretKey.trim(),
      });
      setStatus(res.data);
      setApiKey("");
      setSecretKey("");
      setOpen(false);
      toast.success(`Alpaca paper connected · acct ${res.data?.account_number || "—"}`);
      refresh();
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  };

  const ping = async () => {
    setBusy(true);
    try {
      const res = await api.post("/admin/alpaca/test");
      toast.success(`Ping ok · equity ${fmtUSD(res.data?.ping?.equity)}`);
      refresh();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    if (!window.confirm("Disconnect Alpaca paper? Keys will be wiped from MC.")) return;
    setBusy(true);
    try {
      await api.delete("/admin/alpaca/disconnect");
      toast.success("Alpaca disconnected");
      refresh();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    } finally {
      setBusy(false);
    }
  };

  const connected = status?.connected;

  return (
    <Card className="mb-4" testid="alpaca-tile">
      <div className="flex items-baseline justify-between mb-3">
        <div className="flex items-baseline gap-2">
          <Plug size={13} weight="bold" className="text-rd-text" />
          <span className="label-eyebrow">Broker · Alpaca Paper</span>
          {connected ? (
            <Badge color="#22C55E" testid="alpaca-badge-connected">CONNECTED</Badge>
          ) : (
            <Badge color="#A1A1AA" testid="alpaca-badge-disconnected">NOT CONNECTED</Badge>
          )}
        </div>
        <div className="flex items-center gap-2">
          {connected && (
            <>
              <button
                onClick={ping}
                disabled={busy}
                data-testid="alpaca-ping"
                className="px-2 py-1 text-[10px] font-mono uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-text disabled:opacity-50"
              >
                <Pulse size={10} weight="bold" className="inline mr-1" />
                ping
              </button>
              <button
                onClick={disconnect}
                disabled={busy}
                data-testid="alpaca-disconnect"
                className="px-2 py-1 text-[10px] font-mono uppercase tracking-wider border border-rd-danger text-rd-danger hover:bg-rd-danger hover:text-black disabled:opacity-50"
              >
                <Trash size={10} weight="bold" className="inline mr-1" />
                disconnect
              </button>
            </>
          )}
          <Button
            size="sm"
            variant={connected ? "secondary" : "default"}
            onClick={() => setOpen(true)}
            data-testid="alpaca-connect-trigger"
          >
            <KeyholeIcon size={12} weight="bold" className="mr-1" />
            {connected ? "Rotate Keys" : "Connect Alpaca"}
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-[11px] font-mono">
        <Stat label="Account" value={status?.account_number || "—"} testid="alpaca-stat-acct" />
        <Stat label="Equity" value={fmtUSD(status?.last_equity_snapshot)} testid="alpaca-stat-equity" />
        <Stat
          label="Today $"
          value={`${fmtUSD(caps?.today?.spent_usd)} / ${fmtUSD(caps?.caps?.per_day_usd)}`}
          testid="alpaca-stat-today"
        />
        <Stat
          label="Open Notional"
          value={`${fmtUSD(caps?.open?.open_notional_usd)} / ${fmtUSD(caps?.caps?.open_notional_usd)}`}
          testid="alpaca-stat-open"
        />
        <Stat
          label="Last Ping"
          value={status?.last_ping_at ? `${relTime(status.last_ping_at)} ${status.last_ping_ok ? "✓" : "✗"}` : "—"}
          testid="alpaca-stat-ping"
        />
      </div>

      {connected && (
        <div className="mt-3 text-[10px] font-mono text-rd-muted flex items-center gap-2">
          <ShieldCheck size={11} weight="bold" />
          paper-api.alpaca.markets · keys encrypted at rest ·{" "}
          <span className="text-rd-text">{status.api_key_preview}</span>
          {" · caps "}
          <span className="text-rd-text">
            ${caps?.caps?.per_order_usd}/order · ${caps?.caps?.per_day_usd}/day · ${caps?.caps?.open_notional_usd} open
          </span>
        </div>
      )}

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="bg-rd-bg2 border-rd-border max-w-lg">
          <DialogHeader>
            <DialogTitle className="flex items-baseline gap-2">
              <KeyholeIcon size={16} weight="bold" />
              {connected ? "Rotate Alpaca paper keys" : "Connect Alpaca paper"}
            </DialogTitle>
            <DialogDescription className="text-rd-dim text-[11px] font-mono">
              Use your <span className="text-rd-text">paper-trading</span> credentials only (starts with <span className="text-rd-text">PK</span>).
              Stored Fernet-encrypted on MC; never echoed back to the UI.
              Endpoint: <span className="text-rd-text">https://paper-api.alpaca.markets</span>
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3 py-2">
            <div>
              <Label htmlFor="alpaca-key" className="text-[10px] uppercase tracking-widest text-rd-dim">
                API Key ID
              </Label>
              <Input
                id="alpaca-key"
                data-testid="alpaca-input-key"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="PK… (paper key ID)"
                className="font-mono text-xs bg-rd-bg border-rd-border"
                autoComplete="off"
              />
            </div>
            <div>
              <Label htmlFor="alpaca-secret" className="text-[10px] uppercase tracking-widest text-rd-dim">
                Secret Key
              </Label>
              <Input
                id="alpaca-secret"
                data-testid="alpaca-input-secret"
                value={secretKey}
                onChange={(e) => setSecretKey(e.target.value)}
                placeholder="paper secret"
                type="password"
                className="font-mono text-xs bg-rd-bg border-rd-border"
                autoComplete="off"
              />
            </div>
            {err && (
              <div className="border border-rd-danger text-rd-danger px-3 py-2 text-[11px] font-mono" data-testid="alpaca-error">
                {err}
              </div>
            )}
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)} disabled={busy}>
              Cancel
            </Button>
            <Button
              onClick={connect}
              disabled={busy || !apiKey.trim() || !secretKey.trim()}
              data-testid="alpaca-submit"
            >
              {busy ? <ArrowsClockwise size={12} className="animate-spin mr-1" /> : null}
              {connected ? "Rotate" : "Connect"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function Stat({ label, value, testid }) {
  return (
    <div className="border border-rd-border bg-rd-bg p-2" data-testid={testid}>
      <div className="text-[9px] uppercase tracking-widest text-rd-dim mb-0.5">{label}</div>
      <div className="text-rd-text truncate" title={String(value)}>{value}</div>
    </div>
  );
}
