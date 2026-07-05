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
import { KeyholeIcon, ShieldCheck, Warning, Trash, ArrowsClockwise } from "@phosphor-icons/react";
import { toast } from "sonner";

const REGION_OPTIONS = ["us", "hk", "jp"];
const ENV_OPTIONS = ["pro", "paper"];

/**
 * WebullConnect — App Key / App Secret / Account ID input modal +
 * status pill. Mirror of KrakenConnect.jsx. Doctrine:
 *   - App secret is only shown once (paste-only field). Never rendered
 *     back to the UI after save.
 *   - Backend probes /accounts/{id} live before persisting so bad keys
 *     fail fast with Webull's actual rejection reason.
 *   - Successful save hot-hydrates the running process env — no
 *     supervisor restart needed for the trader threads.
 *   - NOT the 2FA access-token layer. Token creation still happens via
 *     the "Webull 2FA token" strip in SpreadWatcher (POST
 *     /admin/trader/webull-token-create) AFTER the app-key/secret is
 *     in place here.
 */
export default function WebullConnect({ onChange }) {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState(null);
  const [loadingStatus, setLoadingStatus] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/webull/status");
      setStatus(data);
      onChange?.(data);
    } catch (e) {
      if (e?.response?.status !== 404) {
        toast.error(e?.response?.data?.detail || e.message);
      }
    } finally {
      setLoadingStatus(false);
    }
  }, [onChange]);

  useEffect(() => { refresh(); }, [refresh]);

  const connected = status?.connected;
  const envOnly = !connected && status?.env_configured;

  return (
    <>
      <div className="flex items-baseline gap-2" data-testid="webull-connect-block">
        <Button
          size="sm"
          variant={connected ? "secondary" : "default"}
          onClick={() => setOpen(true)}
          data-testid="webull-connect-trigger"
        >
          {connected ? "Manage Webull" : "Connect Webull"}
        </Button>
        {connected && (
          <Badge color={status.environment === "pro" ? "#F59E0B" : "#22C55E"}>
            {(status.environment || "pro").toUpperCase()} · {(status.region_id || "us").toUpperCase()}
          </Badge>
        )}
        {envOnly && (
          <Badge color="#71717A" testid="webull-env-only-chip">
            ENV-ONLY
          </Badge>
        )}
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="bg-rd-bg2 border-rd-border max-w-2xl">
          <DialogHeader>
            <DialogTitle className="flex items-baseline gap-2">
              <KeyholeIcon size={16} weight="bold" />
              {connected ? "Webull · connected" : "Connect Webull"}
            </DialogTitle>
            <DialogDescription className="text-rd-dim text-[11px] font-mono">
              App Key + App Secret + Account ID. Secret stored
              encrypted at rest, never returned. This is the LOWER
              credential layer — you still need to run the 2FA push
              (from the Spread Watcher) to mint an access token before
              quote / trade endpoints activate.
            </DialogDescription>
          </DialogHeader>

          {!loadingStatus && (
            connected
              ? <ConnectedView status={status} onChange={refresh} onClose={() => setOpen(false)} />
              : <ConnectForm envOnly={envOnly} onSaved={() => { refresh(); setOpen(false); }} />
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}

function ConnectForm({ envOnly, onSaved }) {
  const [appKey, setAppKey] = useState("");
  const [appSecret, setAppSecret] = useState("");
  const [accountId, setAccountId] = useState("");
  const [regionId, setRegionId] = useState("us");
  const [environment, setEnvironment] = useState("pro");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    setErr("");
    if (!appKey.trim() || !appSecret.trim() || !accountId.trim()) {
      setErr("app_key, app_secret and account_id are all required");
      return;
    }
    setSubmitting(true);
    try {
      await api.post("/admin/webull/connect", {
        app_key: appKey.trim(),
        app_secret: appSecret.trim(),
        account_id: accountId.trim(),
        region_id: regionId,
        environment,
      });
      toast.success("Webull connected — creds live for next trader tick");
      setAppKey(""); setAppSecret(""); setAccountId("");
      onSaved?.();
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-4 text-sm">
      {envOnly && (
        <div className="border border-rd-warning/40 bg-rd-warning/5 px-3 py-2 text-[11px] font-mono text-rd-warning flex gap-2">
          <Warning size={14} weight="bold" />
          <div>
            Webull creds are currently supplied via <span className="font-bold">.env</span>. Saving here
            promotes them into the Mongo singleton — future deploys read
            them from Mongo, no more .env edits required.
          </div>
        </div>
      )}

      <div>
        <Label htmlFor="webull-app-key" className="text-[10px] uppercase tracking-widest text-rd-dim">
          App Key
        </Label>
        <Input
          id="webull-app-key"
          data-testid="webull-app-key-input"
          value={appKey}
          onChange={(e) => setAppKey(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          placeholder="from Webull Developer Portal → Applications"
          className="font-mono text-xs bg-rd-bg3 border-rd-border"
        />
      </div>

      <div>
        <Label htmlFor="webull-app-secret" className="text-[10px] uppercase tracking-widest text-rd-dim">
          App Secret
        </Label>
        <Input
          id="webull-app-secret"
          data-testid="webull-app-secret-input"
          type="password"
          value={appSecret}
          onChange={(e) => setAppSecret(e.target.value)}
          autoComplete="new-password"
          spellCheck={false}
          placeholder="paste once — never shown again"
          className="font-mono text-xs bg-rd-bg3 border-rd-border"
        />
      </div>

      <div>
        <Label htmlFor="webull-account-id" className="text-[10px] uppercase tracking-widest text-rd-dim">
          Account ID
        </Label>
        <Input
          id="webull-account-id"
          data-testid="webull-account-id-input"
          value={accountId}
          onChange={(e) => setAccountId(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          placeholder="Webull brokerage account identifier"
          className="font-mono text-xs bg-rd-bg3 border-rd-border"
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-1.5">Region</div>
          <div className="flex gap-1.5">
            {REGION_OPTIONS.map((r) => (
              <button
                key={r}
                type="button"
                onClick={() => setRegionId(r)}
                className={`px-2 py-1 text-[11px] font-mono border ${
                  regionId === r
                    ? "border-rd-text text-rd-text bg-rd-bg3"
                    : "border-rd-border text-rd-dim hover:text-rd-text"
                }`}
                data-testid={`webull-region-${r}`}
              >
                {r.toUpperCase()}
              </button>
            ))}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-1.5">Environment</div>
          <div className="flex gap-1.5">
            {ENV_OPTIONS.map((e) => (
              <button
                key={e}
                type="button"
                onClick={() => setEnvironment(e)}
                className={`px-2 py-1 text-[11px] font-mono border ${
                  environment === e
                    ? "border-rd-text text-rd-text bg-rd-bg3"
                    : "border-rd-border text-rd-dim hover:text-rd-text"
                }`}
                data-testid={`webull-environment-${e}`}
              >
                {e.toUpperCase()}
              </button>
            ))}
          </div>
        </div>
      </div>

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 text-[11px] font-mono" data-testid="webull-connect-error">
          {err}
        </div>
      )}

      <DialogFooter className="flex items-center justify-between gap-2">
        <span className="text-[10px] text-rd-dim font-mono">
          Structural check only. Real validation happens on your next 2FA push.
        </span>
        <Button
          onClick={submit}
          disabled={submitting}
          data-testid="webull-save-btn"
          className="bg-rd-text text-rd-bg hover:bg-rd-muted"
        >
          {submitting ? "SAVING…" : "SAVE CREDS"}
        </Button>
      </DialogFooter>
    </div>
  );
}

function ConnectedView({ status, onChange, onClose }) {
  const [busy, setBusy] = useState("");

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

  const probeOk = status.last_probe?.ok;
  const probeTs = status.last_probe?.ts;

  return (
    <div className="space-y-3 text-sm">
      <div className="grid grid-cols-2 gap-3 text-[11px] font-mono">
        <KV label="App Key" value={status.app_key_preview} />
        <KV label="App Secret" value={status.app_secret_preview} />
        <KV label="Account" value={status.account_id_preview} />
        <KV label="Region · Env" value={`${(status.region_id || "us").toUpperCase()} · ${(status.environment || "pro").toUpperCase()}`} />
        <KV label="Connected by" value={status.connected_by} />
        <KV label="Updated" value={status.updated_at ? new Date(status.updated_at).toLocaleString() : "—"} />
      </div>

      <Card className="p-3">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2 flex items-baseline gap-2">
          <ShieldCheck size={11} weight="bold" />
          Auth state
        </div>
        <div className="text-[11px] font-mono flex items-baseline gap-2 flex-wrap" data-testid="webull-last-probe">
          {probeTs ? (
            <>
              <Badge color={probeOk ? "#22C55E" : "#F59E0B"}>
                {probeOk ? "TOKEN LIVE" : "TOKEN MISSING"}
              </Badge>
              <span className="text-rd-dim">
                checked {new Date(probeTs).toLocaleString()}
              </span>
              {status.last_probe?.token_expires_in_hours != null && (
                <span className="text-rd-text">
                  · expires in {status.last_probe.token_expires_in_hours}h
                </span>
              )}
            </>
          ) : (
            <Badge color="#71717A">NOT PROBED YET</Badge>
          )}
        </div>
        {!probeOk && status.last_probe?.hint && (
          <div className="text-rd-dim mt-1 text-[11px] font-mono">
            {status.last_probe.hint}
          </div>
        )}
      </Card>

      <DialogFooter className="flex items-center justify-between gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={() => action("Re-probe", () => api.post("/admin/webull/probe"))}
          disabled={busy !== ""}
          data-testid="webull-reprobe-btn"
        >
          <ArrowsClockwise size={12} weight="bold" className="mr-1" />
          {busy === "Re-probe" ? "…" : "Re-probe"}
        </Button>
        <Button
          variant="destructive"
          size="sm"
          onClick={async () => {
            if (!confirm("Disconnect Webull? Stored keys will be deleted.")) return;
            await action("Disconnect", () => api.delete("/admin/webull/disconnect"));
            onClose?.();
          }}
          disabled={busy !== ""}
          data-testid="webull-disconnect-btn"
        >
          <Trash size={12} weight="bold" className="mr-1" /> Disconnect
        </Button>
      </DialogFooter>
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
