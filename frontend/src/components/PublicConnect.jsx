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
import { KeyholeIcon, ShieldCheck, Warning, Trash, ArrowsClockwise, Lightning } from "@phosphor-icons/react";
import { toast } from "sonner";

/**
 * Public.com connection panel — long-lived SECRET → short-lived
 * ACCESS_TOKEN. Background refresher rolls the token before expiry so
 * operator-issued calls never wait on the exchange. Doctrine: trade
 * endpoints remain unwired in Phase 1; execution defaults off.
 */
export default function PublicConnect() {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState(null);
  const [loadingStatus, setLoadingStatus] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/public/status");
      setStatus(data);
    } catch (e) {
      if (e?.response?.status !== 404) {
        toast.error(e?.response?.data?.detail || e.message);
      }
    } finally {
      setLoadingStatus(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const connected = status?.connected;
  return (
    <>
      <div className="flex items-baseline gap-2" data-testid="public-connect-block">
        <Button
          size="sm"
          variant={connected ? "secondary" : "default"}
          onClick={() => setOpen(true)}
          data-testid="public-connect-trigger"
        >
          {connected ? "Manage Public" : "Connect Public.com"}
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
              {connected ? "Public.com · connected" : "Connect Public.com"}
            </DialogTitle>
            <DialogDescription className="text-rd-dim text-[11px] font-mono">
              Long-lived SECRET stored encrypted at rest; short-lived
              ACCESS TOKEN refreshed in the background. READ-ONLY by
              default; execution stays schema-pinned off until explicitly
              authorized.
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
  const [secret, setSecret] = useState("");
  const [accountId, setAccountId] = useState("");
  const [baseUrl, setBaseUrl] = useState("https://api.public.com");
  const [validity, setValidity] = useState(1440);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    setErr("");
    if (!secret.trim()) { setErr("secret required"); return; }
    setSubmitting(true);
    try {
      await api.post("/admin/public/connect", {
        secret: secret.trim(),
        account_id: accountId.trim() || null,
        base_url: baseUrl.trim(),
        token_validity_minutes: parseInt(validity, 10) || 1440,
      });
      toast.success("Public.com connected — token refresher running");
      setSecret(""); setAccountId("");
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
          Generate a secret key at{" "}
          <span className="text-rd-text">public.com/settings/security/api</span>.
          Public has no PDT restrictions on cash accounts — this slot is
          a candidate executor venue once Phase 2 ships. Trade endpoints
          are NOT wired yet.
        </div>
      </div>

      <div>
        <Label htmlFor="public-secret" className="text-[10px] uppercase tracking-widest text-rd-dim">
          secret key
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

      <div>
        <Label htmlFor="public-account" className="text-[10px] uppercase tracking-widest text-rd-dim">
          account_id <span className="text-rd-muted">· optional, auto-detected when only one account</span>
        </Label>
        <Input
          id="public-account"
          data-testid="public-account-input"
          value={accountId}
          onChange={(e) => setAccountId(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          placeholder="e.g. ABC123 — only needed for multi-account setups"
          className="font-mono text-xs bg-rd-bg3 border-rd-border"
        />
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <Label htmlFor="public-base" className="text-[10px] uppercase tracking-widest text-rd-dim">
            base_url
          </Label>
          <Input
            id="public-base"
            data-testid="public-base-input"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            autoComplete="off"
            spellCheck={false}
            className="font-mono text-xs bg-rd-bg3 border-rd-border"
          />
        </div>
        <div>
          <Label htmlFor="public-validity" className="text-[10px] uppercase tracking-widest text-rd-dim">
            token validity (minutes)
          </Label>
          <Input
            id="public-validity"
            data-testid="public-validity-input"
            type="number"
            min="5"
            max="10080"
            value={validity}
            onChange={(e) => setValidity(e.target.value)}
            className="font-mono text-xs bg-rd-bg3 border-rd-border"
          />
        </div>
      </div>

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 text-[11px] font-mono">
          {err}
        </div>
      )}

      <DialogFooter className="flex items-center justify-between gap-2">
        <span className="text-[10px] text-rd-dim font-mono">
          We exchange the secret for an access token + probe accounts before persisting.
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
  const [portfolio, setPortfolio] = useState(null);

  const action = async (label, fn) => {
    setBusy(label);
    try {
      const r = await fn();
      toast.success(`${label} OK`);
      if (label === "Load portfolio") setPortfolio(r?.data);
      await onChange();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    } finally {
      setBusy("");
    }
  };

  const expiry = status.access_token_expires_at
    ? new Date(status.access_token_expires_at)
    : null;
  const expiryMinutes = expiry
    ? Math.max(0, Math.round((expiry - new Date()) / 60000))
    : null;

  return (
    <div className="space-y-3 text-sm">
      <div className="grid grid-cols-2 gap-3 text-[11px] font-mono">
        <KV label="Base URL" value={status.base_url} />
        <KV label="Secret" value={status.secret_preview} />
        <KV label="Account" value={status.account_id} />
        <KV label="Connected by" value={status.connected_by} />
      </div>

      <Card className="p-3 text-[11px] font-mono">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2 flex items-baseline gap-2">
          <ShieldCheck size={11} weight="bold" />
          Access token
        </div>
        <div>
          Validity: <span className="text-rd-text">{status.token_validity_minutes}m</span>
          {expiry && (
            <>
              {" · "}
              expires in{" "}
              <span className={expiryMinutes < 10 ? "text-rd-warning" : "text-rd-text"}>
                {expiryMinutes}m
              </span>
              {" "}
              <span className="text-rd-dim">({expiry.toLocaleTimeString()})</span>
            </>
          )}
        </div>
        <div className="mt-1">
          Refresher:{" "}
          <Badge color={status.refresher_running ? "#22C55E" : "#A1A1AA"}>
            {status.refresher_running ? "RUNNING" : "IDLE"}
          </Badge>
          {status.last_refresh?.ts && (
            <span className="ml-2 text-rd-dim">
              last refresh {new Date(status.last_refresh.ts).toLocaleTimeString()} ·
              {status.last_refresh.ok ? " ok" : " FAIL"}
            </span>
          )}
        </div>
        {status.last_refresh?.error && (
          <div className="text-rd-danger mt-1">refresh error: {status.last_refresh.error}</div>
        )}
      </Card>

      {(status.accounts || []).length > 0 && (
        <Card className="p-3 text-[11px] font-mono">
          <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2">
            Detected accounts
          </div>
          <div className="space-y-0.5">
            {status.accounts.map((a) => (
              <div key={a.id} className="flex items-baseline gap-2">
                <Badge color={a.id === status.account_id ? "#22C55E" : "#A1A1AA"}>
                  {a.id === status.account_id ? "ACTIVE" : "—"}
                </Badge>
                <span className="text-rd-text">{a.id}</span>
                {a.type && <span className="text-rd-dim">· {a.type}</span>}
                {a.brokerage_type && <span className="text-rd-dim">· {a.brokerage_type}</span>}
                {a.options_level && a.options_level !== "NONE" && (
                  <span className="text-rd-dim">· opt {a.options_level}</span>
                )}
                {a.permissions && (
                  <span className="text-rd-dim ml-auto">{a.permissions}</span>
                )}
              </div>
            ))}
          </div>
        </Card>
      )}

      {portfolio && (
        <Card className="p-3 text-[11px] font-mono">
          <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2">
            Portfolio (account {portfolio.account_id})
          </div>
          <pre className="text-rd-text text-[10px] whitespace-pre-wrap break-all max-h-48 overflow-y-auto">
            {JSON.stringify(portfolio.portfolio, null, 2).slice(0, 1500)}
          </pre>
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
              <span className="text-rd-warning">ENABLED — trade endpoints unlocked for future use</span>
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
          {busy === "Test" ? "…" : "Test auth"}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => action("Refresh token", () => api.post("/admin/public/refresh-token"))}
          disabled={busy !== ""}
          data-testid="public-refresh-btn"
        >
          <ArrowsClockwise size={12} weight="bold" className="mr-1" />
          {busy === "Refresh token" ? "…" : "Refresh token"}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => action("Load portfolio", () => api.get("/admin/public/portfolio"))}
          disabled={busy !== "" || !status.account_id}
          data-testid="public-portfolio-btn"
        >
          {busy === "Load portfolio" ? "…" : "Load portfolio"}
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
      await api.post("/admin/public/execution", { enabled: newState, confirm: phrase });
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
        data-testid="public-exec-confirm-input"
      />
      <Button
        size="sm"
        onClick={submit}
        disabled={submitting || phrase !== expectedPhrase}
        data-testid="public-exec-confirm-btn"
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
