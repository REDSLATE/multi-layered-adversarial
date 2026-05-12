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
 * IBKR connection panel — same UX pattern as KrakenConnect, OAuth 2.0
 * Bearer-token auth against api.ibkr.com.
 */
export default function IBKRConnect() {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState(null);
  const [loadingStatus, setLoadingStatus] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/ibkr/status");
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
      <div className="flex items-baseline gap-2" data-testid="ibkr-connect-block">
        <Button
          size="sm"
          variant={connected ? "secondary" : "default"}
          onClick={() => setOpen(true)}
          data-testid="ibkr-connect-trigger"
        >
          {connected ? "Manage IBKR" : "Connect IBKR"}
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
              {connected ? "IBKR · connected" : "Connect IBKR Web API"}
            </DialogTitle>
            <DialogDescription className="text-rd-dim text-[11px] font-mono">
              OAuth 2.0 Bearer token. Stored encrypted at rest, never
              returned. READ-ONLY by default; execution stays
              schema-pinned off until explicitly authorized.
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
  const [accessToken, setAccessToken] = useState("");
  const [accountId, setAccountId] = useState("");
  const [baseUrl, setBaseUrl] = useState("https://api.ibkr.com");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    setErr("");
    if (!accessToken.trim()) {
      setErr("access_token required");
      return;
    }
    setSubmitting(true);
    try {
      await api.post("/admin/ibkr/connect", {
        access_token: accessToken.trim(),
        account_id: accountId.trim() || null,
        base_url: baseUrl.trim(),
      });
      toast.success("IBKR connected — tickler running");
      setAccessToken(""); setAccountId("");
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
          The IBKR Web API session must already be initialised on the
          IBKR side (gateway/SSO logged in at least once). Tokens expire —
          you may need to refresh periodically. Trade endpoints are NOT
          wired yet — execution stays gated until a separate change.
        </div>
      </div>

      <div>
        <Label htmlFor="ibkr-token" className="text-[10px] uppercase tracking-widest text-rd-dim">
          access_token
        </Label>
        <Input
          id="ibkr-token"
          data-testid="ibkr-token-input"
          type="password"
          value={accessToken}
          onChange={(e) => setAccessToken(e.target.value)}
          autoComplete="new-password"
          spellCheck={false}
          placeholder="Bearer token from IBKR OAuth flow"
          className="font-mono text-xs bg-rd-bg3 border-rd-border"
        />
      </div>

      <div>
        <Label htmlFor="ibkr-account" className="text-[10px] uppercase tracking-widest text-rd-dim">
          account_id <span className="text-rd-muted">· optional, auto-detected when only one account</span>
        </Label>
        <Input
          id="ibkr-account"
          data-testid="ibkr-account-input"
          value={accountId}
          onChange={(e) => setAccountId(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          placeholder="e.g. DU123456 (paper) or U1234567 (live)"
          className="font-mono text-xs bg-rd-bg3 border-rd-border"
        />
      </div>

      <div>
        <Label htmlFor="ibkr-base" className="text-[10px] uppercase tracking-widest text-rd-dim">
          base_url
        </Label>
        <Input
          id="ibkr-base"
          data-testid="ibkr-base-input"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          className="font-mono text-xs bg-rd-bg3 border-rd-border"
        />
      </div>

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 text-[11px] font-mono">
          {err}
        </div>
      )}

      <DialogFooter className="flex items-center justify-between gap-2">
        <span className="text-[10px] text-rd-dim font-mono">
          We probe /iserver/auth/status before persisting. Bad tokens fail loud.
        </span>
        <Button
          onClick={submit}
          disabled={submitting}
          data-testid="ibkr-save-btn"
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
  const [positions, setPositions] = useState(null);

  const action = async (label, fn) => {
    setBusy(label);
    try {
      const r = await fn();
      toast.success(`${label} OK`);
      if (label === "Load positions") setPositions(r?.data);
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
        <KV label="Base URL" value={status.base_url} />
        <KV label="Token" value={status.token_preview} />
        <KV label="Account" value={status.account_id} />
        <KV label="Connected by" value={status.connected_by} />
      </div>

      <Card className="p-3 text-[11px] font-mono">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2 flex items-baseline gap-2">
          <ShieldCheck size={11} weight="bold" />
          Session
        </div>
        <div>
          Auth status:{" "}
          <Badge color={status.auth_status?.authenticated ? "#22C55E" : "#DC2626"}>
            {status.auth_status?.authenticated ? "AUTHENTICATED" : "NOT AUTHENTICATED"}
          </Badge>
          {status.auth_status?.competing != null && (
            <span className="ml-2">
              competing:{" "}
              <span className="text-rd-text">{String(status.auth_status.competing)}</span>
            </span>
          )}
        </div>
        <div className="mt-1">
          Tickler:{" "}
          <Badge color={status.tickler_running ? "#22C55E" : "#A1A1AA"}>
            {status.tickler_running ? "RUNNING" : "IDLE"}
          </Badge>
          {status.last_tickle?.ts && (
            <span className="ml-2 text-rd-dim">
              last tickle {new Date(status.last_tickle.ts).toLocaleTimeString()} ·
              {status.last_tickle.ok ? " ok" : " FAIL"}
            </span>
          )}
        </div>
        {status.last_tickle?.error && (
          <div className="text-rd-danger mt-1">tickle error: {status.last_tickle.error}</div>
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
                {a.alias && <span className="text-rd-dim">· {a.alias}</span>}
                {a.type && <span className="text-rd-dim">· {a.type}</span>}
              </div>
            ))}
          </div>
        </Card>
      )}

      {positions && (
        <Card className="p-3 text-[11px] font-mono">
          <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2">
            Positions (page 0)
          </div>
          {(positions.items || []).length === 0 ? (
            <div className="text-rd-dim">no open positions</div>
          ) : (
            <div className="space-y-0.5">
              {(positions.items || []).slice(0, 10).map((p, i) => (
                <div
                  key={p.conid ?? p.contract_id ?? `${p.contractDesc || p.contract_desc || "pos"}-${i}`}
                  className="flex items-baseline gap-2"
                >
                  <span className="text-rd-text">{p.contractDesc || p.contract_desc || p.conid}</span>
                  <span className="text-rd-dim">qty {p.position}</span>
                  <span className="text-rd-dim ml-auto">{p.mktValue ?? p.market_value}</span>
                </div>
              ))}
            </div>
          )}
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
            data-testid="ibkr-exec-toggle-btn"
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
          onClick={() => action("Test", () => api.post("/admin/ibkr/test"))}
          disabled={busy !== ""}
          data-testid="ibkr-test-btn"
        >
          {busy === "Test" ? "…" : "Test auth"}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => action("Tickle", () => api.post("/admin/ibkr/tickle"))}
          disabled={busy !== ""}
          data-testid="ibkr-tickle-btn"
        >
          {busy === "Tickle" ? "…" : "Tickle now"}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => action("Load positions", () => api.get("/admin/ibkr/positions"))}
          disabled={busy !== "" || !status.account_id}
          data-testid="ibkr-positions-btn"
        >
          {busy === "Load positions" ? "…" : "Load positions"}
        </Button>
        <Button
          variant="destructive"
          size="sm"
          onClick={async () => {
            if (!confirm("Disconnect IBKR? Stored token will be deleted.")) return;
            await action("Disconnect", () => api.delete("/admin/ibkr/disconnect"));
            onClose?.();
          }}
          disabled={busy !== ""}
          data-testid="ibkr-disconnect-btn"
        >
          <Trash size={12} weight="bold" className="mr-1" /> Disconnect
        </Button>
      </DialogFooter>
    </div>
  );
}

function ExecutionToggle({ currentlyEnabled, onDone }) {
  const newState = !currentlyEnabled;
  const expectedPhrase = newState ? "I authorize execution on IBKR" : "Disable execution";
  const [phrase, setPhrase] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    setSubmitting(true);
    try {
      await api.post("/admin/ibkr/execution", { enabled: newState, confirm: phrase });
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
        data-testid="ibkr-exec-confirm-input"
      />
      <Button
        size="sm"
        onClick={submit}
        disabled={submitting || phrase !== expectedPhrase}
        data-testid="ibkr-exec-confirm-btn"
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
