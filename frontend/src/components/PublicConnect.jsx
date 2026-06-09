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
import {
  KeyholeIcon, ShieldCheck, Warning, Trash, ArrowsClockwise, Lightning, Plug, Pulse,
} from "@phosphor-icons/react";
import { toast } from "sonner";

/**
 * Public.com connection panel — replaces the legacy Alpaca tile on the
 * Equity Lane. Long-lived SECRET stored encrypted at rest; short-lived
 * ACCESS TOKEN refreshed in the background. Doctrine: execution stays
 * OFF by default — operator must explicitly toggle with typed-phrase
 * confirmation. Keys NEVER round-trip back to the browser plaintext.
 */
function fmtUSD(v) {
  if (v == null) return "—";
  const n = Number(v);
  if (isNaN(n)) return "—";
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

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

function shortenId(id) {
  if (!id) return null;
  if (id.length <= 14) return id;
  return `${id.slice(0, 6)}…${id.slice(-4)}`;
}

export default function PublicConnect({ onChange }) {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState(null);
  const [caps, setCaps] = useState(null);
  const [loadingStatus, setLoadingStatus] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const [s, c] = await Promise.all([
        api.get("/admin/public/status"),
        api.get("/execution/caps").catch(() => ({ data: null })),
      ]);
      setStatus(s.data);
      setCaps(c.data);
      onChange?.(s.data);
    } catch (e) {
      if (e?.response?.status !== 404) {
        toast.error(e?.response?.data?.detail || e.message);
      }
    } finally {
      setLoadingStatus(false);
    }
  }, [onChange]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 20000);
    return () => clearInterval(t);
  }, [refresh]);

  const connected = status?.connected;

  return (
    <Card className="mb-4" testid="public-tile">
      <div className="flex items-baseline justify-between mb-3 flex-wrap gap-2">
        <div className="flex items-baseline gap-2">
          <Plug size={13} weight="bold" className="text-rd-text" />
          <span className="label-eyebrow">Broker · Public.com</span>
          {connected ? (
            <Badge color="#22C55E" testid="public-badge-connected">CONNECTED</Badge>
          ) : (
            <Badge color="#A1A1AA" testid="public-badge-disconnected">NOT CONNECTED</Badge>
          )}
          {connected && (
            <Badge color={status.execution_enabled ? "#F59E0B" : "#22C55E"}>
              {status.execution_enabled ? "EXEC ENABLED" : "READ-ONLY"}
            </Badge>
          )}
        </div>
        <Button
          size="sm"
          variant={connected ? "secondary" : "default"}
          onClick={() => setOpen(true)}
          data-testid="public-connect-trigger"
        >
          <KeyholeIcon size={12} weight="bold" className="mr-1" />
          {connected ? "Manage Public" : "Connect Public"}
        </Button>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-[11px] font-mono">
        <Stat
          label="Account"
          value={shortenId(status?.account_id) || "—"}
          testid="public-stat-acct"
        />
        <Stat
          label="Secret"
          value={status?.secret_preview || "—"}
          testid="public-stat-secret"
        />
        <Stat
          label="Today $"
          value={`${fmtUSD(caps?.today?.spent_usd)} / ${fmtUSD(caps?.caps?.per_day_usd)}`}
          testid="public-stat-today"
        />
        <Stat
          label="Open Notional"
          value={`${fmtUSD(caps?.open?.open_notional_usd)} / ${fmtUSD(caps?.caps?.open_notional_usd)}`}
          testid="public-stat-open"
        />
        <Stat
          label="Token Refresh"
          value={
            status?.access_token_refreshed_at
              ? relTime(status.access_token_refreshed_at)
              : "—"
          }
          testid="public-stat-token"
        />
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="bg-rd-bg2 border-rd-border max-w-2xl">
          <DialogHeader>
            <DialogTitle className="flex items-baseline gap-2">
              <KeyholeIcon size={16} weight="bold" />
              {connected ? "Public.com · connected" : "Connect Public.com"}
            </DialogTitle>
            <DialogDescription className="text-rd-dim text-[11px] font-mono">
              Long-lived SECRET stored encrypted at rest; short-lived
              ACCESS TOKEN refreshed in the background. Default permission
              is READ-ONLY — execution stays schema-pinned off until
              explicitly authorized.
            </DialogDescription>
          </DialogHeader>

          {!loadingStatus && (
            connected
              ? <ConnectedView status={status} onChange={refresh} onClose={() => setOpen(false)} />
              : <ConnectForm onSaved={() => { refresh(); setOpen(false); }} />
          )}
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function Stat({ label, value, testid }) {
  return (
    <div data-testid={testid}>
      <div className="text-[10px] uppercase tracking-widest text-rd-dim">{label}</div>
      <div className="text-rd-text truncate" title={value || "—"}>{value || "—"}</div>
    </div>
  );
}

function ConnectForm({ onSaved }) {
  const [secret, setSecret] = useState("");
  const [baseUrl, setBaseUrl] = useState("https://api.public.com");
  const [accountId, setAccountId] = useState("");
  const [tokenMinutes, setTokenMinutes] = useState(60);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    setErr("");
    if (!secret.trim() || secret.trim().length < 20) {
      setErr("paste your full Public.com API secret (≥ 20 chars)");
      return;
    }
    setSubmitting(true);
    try {
      await api.post("/admin/public/connect", {
        secret: secret.trim(),
        base_url: baseUrl.trim(),
        account_id: accountId.trim() || undefined,
        token_validity_minutes: Number(tokenMinutes) || 60,
      });
      toast.success("Public connected — token cached, refresher running");
      setSecret("");
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
          Get your secret at{" "}
          <span className="text-rd-text">
            public.com → Settings → Security → API
          </span>. Copy with no trailing whitespace. Legacy Alpaca keys
          are unrelated and have been removed from MC.
        </div>
      </div>

      <div>
        <Label htmlFor="public-secret" className="text-[10px] uppercase tracking-widest text-rd-dim">
          API Secret
        </Label>
        <Input
          id="public-secret"
          data-testid="public-secret-input"
          type="password"
          value={secret}
          onChange={(e) => setSecret(e.target.value)}
          autoComplete="new-password"
          spellCheck={false}
          placeholder="paste once — never shown again"
          className="font-mono text-xs bg-rd-bg3 border-rd-border"
        />
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <Label htmlFor="public-account-id" className="text-[10px] uppercase tracking-widest text-rd-dim">
            Account ID <span className="text-rd-dim">(optional)</span>
          </Label>
          <Input
            id="public-account-id"
            data-testid="public-account-id-input"
            value={accountId}
            onChange={(e) => setAccountId(e.target.value)}
            autoComplete="off"
            spellCheck={false}
            placeholder="auto-picked when single account"
            className="font-mono text-xs bg-rd-bg3 border-rd-border"
          />
        </div>
        <div>
          <Label htmlFor="public-token-mins" className="text-[10px] uppercase tracking-widest text-rd-dim">
            Token TTL (minutes)
          </Label>
          <Input
            id="public-token-mins"
            data-testid="public-token-mins-input"
            type="number"
            value={tokenMinutes}
            onChange={(e) => setTokenMinutes(e.target.value)}
            min={5}
            max={10080}
            className="font-mono text-xs bg-rd-bg3 border-rd-border"
          />
        </div>
      </div>

      <div>
        <Label htmlFor="public-base-url" className="text-[10px] uppercase tracking-widest text-rd-dim">
          Base URL
        </Label>
        <Input
          id="public-base-url"
          data-testid="public-base-url-input"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          className="font-mono text-xs bg-rd-bg3 border-rd-border"
        />
      </div>

      {err && (
        <div
          className="border border-rd-danger text-rd-danger px-3 py-2 text-[11px] font-mono"
          data-testid="public-connect-error"
        >
          {err}
        </div>
      )}

      <DialogFooter className="flex items-center justify-between gap-2">
        <span className="text-[10px] text-rd-dim font-mono">
          We exchange the secret for an access token to confirm the
          key is alive, then start the auto-refresher.
        </span>
        <Button
          onClick={submit}
          disabled={submitting}
          data-testid="public-save-btn"
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
      <div className="grid grid-cols-2 gap-3 text-[11px] font-mono">
        <KV label="Secret" value={status.secret_preview} />
        <KV label="Account" value={shortenId(status.account_id)} />
        <KV label="Base URL" value={status.base_url} />
        <KV label="Connected by" value={status.connected_by} />
        <KV
          label="Token refreshed"
          value={status.access_token_refreshed_at
            ? new Date(status.access_token_refreshed_at).toLocaleString()
            : "—"}
        />
        <KV
          label="Token expires"
          value={status.access_token_expires_at
            ? new Date(status.access_token_expires_at).toLocaleString()
            : "—"}
        />
      </div>

      {status.accounts && status.accounts.length > 1 && (
        <Card className="p-3 text-[11px] font-mono">
          <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2">
            Detected accounts
          </div>
          <ul className="space-y-1">
            {status.accounts.map((a) => (
              <li
                key={a.id}
                className="flex items-baseline gap-2"
                data-testid={`public-acct-${a.id}`}
              >
                <Badge color={a.id === status.account_id ? "#22C55E" : "#A1A1AA"}>
                  {a.id === status.account_id ? "ACTIVE" : "—"}
                </Badge>
                <span className="text-rd-text">{a.id}</span>
                {a.type && <span className="text-rd-dim ml-2">{a.type}</span>}
              </li>
            ))}
          </ul>
          <div className="text-[10px] text-rd-dim mt-2">
            To switch active account, reconnect with the new Account ID.
          </div>
        </Card>
      )}

      <Card className="p-3">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2 flex items-baseline gap-2">
          <Lightning size={11} weight="bold" />
          Execution authority
        </div>
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-[11px] font-mono">
            {status.execution_enabled ? (
              <span className="text-rd-warning">ENABLED — equity orders will hit Public</span>
            ) : (
              <span className="text-rd-text">DISABLED · doctrine default</span>
            )}
          </span>
          <Button
            size="sm"
            variant="outline"
            onClick={() => setShowExecToggle(!showExecToggle)}
            data-testid="public-exec-toggle-btn"
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

      <DialogFooter className="flex items-center justify-between gap-2 flex-wrap">
        <Button
          variant="outline"
          size="sm"
          onClick={() => action("Test", () => api.post("/admin/public/test"))}
          disabled={busy !== ""}
          data-testid="public-test-btn"
        >
          {busy === "Test" ? "…" : (
            <>
              <Pulse size={12} weight="bold" className="mr-1" />
              Test connection
            </>
          )}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => action("Refresh token", () => api.post("/admin/public/refresh-token"))}
          disabled={busy !== ""}
          data-testid="public-refresh-btn"
        >
          {busy === "Refresh token" ? "…" : (
            <>
              <ArrowsClockwise size={12} weight="bold" className="mr-1" />
              Refresh token
            </>
          )}
        </Button>
        <Button
          variant="destructive"
          size="sm"
          onClick={async () => {
            if (!confirm("Disconnect Public.com? Stored secret will be deleted.")) return;
            await action("Disconnect", () => api.delete("/admin/public/disconnect"));
            onClose?.();
          }}
          disabled={busy !== ""}
          data-testid="public-disconnect-btn"
        >
          <Trash size={12} weight="bold" className="mr-1" /> Disconnect
        </Button>
      </DialogFooter>
    </div>
  );
}

function ExecutionToggle({ currentlyEnabled, onDone }) {
  const newState = !currentlyEnabled;
  const expectedPhrase = newState ? "I authorize execution on Public" : "Disable execution";
  const [phrase, setPhrase] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    setSubmitting(true);
    try {
      await api.post("/admin/public/execution", {
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

  const matched = phrase === expectedPhrase;

  return (
    <div className="mt-3 border-t border-rd-border pt-3 space-y-2">
      <div className="text-[11px] font-mono text-rd-dim">
        Confirmation required. Type exactly:
      </div>
      <div className="flex items-stretch gap-2">
        <div
          className="flex-1 text-[11px] font-mono text-rd-dim bg-transparent px-2 py-1.5 border border-dashed border-rd-border select-all"
          data-testid="public-exec-required-phrase"
        >
          <span className="text-[9px] uppercase tracking-widest mr-2 text-rd-muted">required phrase</span>
          <span className="text-rd-text">{expectedPhrase}</span>
        </div>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => setPhrase(expectedPhrase)}
          data-testid="public-exec-fill-phrase"
        >
          fill
        </Button>
      </div>
      <Input
        value={phrase}
        onChange={(e) => setPhrase(e.target.value)}
        autoComplete="off"
        spellCheck={false}
        placeholder={`Paste: "${expectedPhrase}"`}
        className="font-mono text-xs bg-rd-bg3 border-rd-border"
        data-testid="public-exec-confirm-input"
      />
      {!matched && phrase.length > 0 && (
        <div className="text-[10px] font-mono text-rd-warning">
          Phrase doesn&apos;t match yet — must be exactly: {expectedPhrase}
        </div>
      )}
      <Button
        size="sm"
        onClick={submit}
        disabled={submitting || !matched}
        data-testid="public-exec-confirm-btn"
        variant={newState ? "destructive" : "default"}
      >
        {submitting
          ? "…"
          : matched
          ? (newState ? "ENABLE EXECUTION" : "DISABLE EXECUTION")
          : (newState ? "ENABLE (phrase required)" : "DISABLE (phrase required)")}
      </Button>
    </div>
  );
}

function KV({ label, value }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-rd-dim">{label}</div>
      <div className="text-rd-text truncate" title={value || "—"}>{value || "—"}</div>
    </div>
  );
}
