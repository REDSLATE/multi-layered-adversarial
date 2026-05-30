import React, { useCallback, useState } from "react";
import { api, relTime } from "@/lib/api";
import { Card } from "@/components/ui-bits";

/**
 * BrainProxiedStatusTile — renders the brain's own `/api/admin/runtime/
 * {brain}/status` payload, proxied through MC.
 *
 * The wrapper shape we receive from `/api/admin/runtime/{brain}/status`:
 *   { brain, ok, ts, doctrine,
 *     _proxied_from?, _proxy_duration_ms?, _proxy_age_s?,
 *     _proxy_from_cache?, payload? }
 *   OR  { brain, ok: false, error, upstream_status_code? }
 *
 * The brain-side `payload` (RedEye's spec) has 8 flat sections:
 *   identity · seats · heartbeat · governor_emitter · data_keys ·
 *   neuro_engine · intents · (per-brain extras)
 *
 * We render each present section as a separate sub-card. Missing
 * sections silently no-op — different brains expose different
 * subsets; the tile must handle partial payloads without crashing.
 */

function Dot({ color, size = 8 }) {
  return (
    <span
      className="inline-block rounded-full"
      style={{ background: color, width: size, height: size }}
    />
  );
}

function KV({ k, v, testid, mono = true }) {
  if (v === undefined) return null;
  const display =
    v === null
      ? "—"
      : typeof v === "boolean"
      ? v ? "true" : "false"
      : typeof v === "object"
      ? JSON.stringify(v)
      : String(v);
  return (
    <div className="flex items-center justify-between text-[11px] py-1 border-b border-rd-border last:border-b-0" data-testid={testid}>
      <span className="text-rd-dim uppercase tracking-widest">{k}</span>
      <span className={`${mono ? "font-mono" : ""} text-rd-text text-right max-w-[60%] truncate`} title={display}>
        {display}
      </span>
    </div>
  );
}

function Section({ title, children, testid }) {
  return (
    <div className="bg-rd-bg3 border border-rd-border p-3" data-testid={testid}>
      <div className="text-[10px] text-rd-warn uppercase tracking-widest font-mono mb-2">
        {title}
      </div>
      <div className="space-y-0">{children}</div>
    </div>
  );
}

export default function BrainProxiedStatusTile({ brain, proxied }) {
  const [refreshing, setRefreshing] = useState(false);
  const [forceRefreshErr, setForceRefreshErr] = useState("");

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    setForceRefreshErr("");
    try {
      await api.post(`/admin/runtime/${brain}/status/refresh`);
      // Soft reload — easier than threading a refresh callback up.
      window.location.reload();
    } catch (e) {
      setForceRefreshErr(e?.response?.data?.detail || e.message);
      setRefreshing(false);
    }
  }, [brain]);

  if (!proxied) {
    return null;
  }

  // Wrapper-level failure (no_upstream_configured / upstream_timeout / etc.)
  if (proxied.ok === false) {
    return (
      <Card testid={`brain-status-proxy-${brain}-error`} className="mb-6">
        <div className="flex items-center justify-between mb-2">
          <div>
            <div className="label-eyebrow">Brain telemetry · MC proxy</div>
            <div className="font-mono text-sm text-rd-warn flex items-center gap-2 mt-1">
              <Dot color="#F59E0B" />
              {proxied.error || "upstream_unavailable"}
            </div>
          </div>
          <button
            onClick={handleRefresh}
            disabled={refreshing}
            className="text-[10px] font-mono uppercase tracking-widest border border-rd-border text-rd-warn hover:text-rd-text px-2 py-1"
            data-testid={`brain-status-proxy-${brain}-retry`}
          >
            {refreshing ? "..." : "↻ retry"}
          </button>
        </div>
        <div className="text-[11px] text-rd-muted font-mono leading-relaxed">
          MC could not fetch <span className="text-rd-text">{brain}</span>'s{" "}
          <code>/status</code> endpoint.{" "}
          {proxied.error === "no_upstream_configured" && (
            <>
              Set <code>{brain.toUpperCase()}_STATUS_URL</code> in MC's env to
              the brain's runtime-status endpoint (e.g.{" "}
              <code>https://{brain}.risedual.ai/api/admin/runtime/{brain}/status</code>),
              then redeploy MC.
            </>
          )}
          {proxied.upstream_status_code && (
            <> Upstream HTTP {proxied.upstream_status_code}.</>
          )}
          {proxied.duration_ms != null && (
            <> Attempt took {Math.round(proxied.duration_ms)}ms.</>
          )}
        </div>
        {forceRefreshErr && (
          <div className="text-xs text-red-400 font-mono mt-2">{forceRefreshErr}</div>
        )}
      </Card>
    );
  }

  // Success path — render whatever sections the brain provided.
  const p = proxied.payload || {};
  const cacheBadge = proxied._proxy_from_cache ? (
    <span className="text-[10px] font-mono uppercase tracking-widest text-rd-dim ml-2">
      cached {proxied._proxy_age_s?.toFixed?.(1) ?? "?"}s
    </span>
  ) : (
    <span className="text-[10px] font-mono uppercase tracking-widest text-emerald-400 ml-2">
      fresh
    </span>
  );

  return (
    <Card testid={`brain-status-proxy-${brain}`} className="mb-6">
      <div className="flex items-start justify-between mb-3">
        <div>
          <div className="label-eyebrow">Brain telemetry · MC proxy</div>
          <div className="font-mono text-sm mt-1 flex items-center gap-2">
            <Dot color="#10B981" />
            <span className="text-rd-text">{brain}</span>
            {cacheBadge}
            <span className="text-[10px] font-mono text-rd-dim ml-2">
              · {Math.round(proxied._proxy_duration_ms || 0)}ms upstream
            </span>
          </div>
          <div className="text-[10px] text-rd-dim font-mono mt-1 truncate max-w-[50ch]" title={proxied._proxied_from}>
            ↗ {proxied._proxied_from}
          </div>
        </div>
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="text-[10px] font-mono uppercase tracking-widest border border-rd-border text-rd-warn hover:text-rd-text px-2 py-1"
          data-testid={`brain-status-proxy-${brain}-refresh`}
        >
          {refreshing ? "..." : "↻ force-refresh"}
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        {p.identity && (
          <Section title="Identity" testid={`proxied-${brain}-identity`}>
            <KV k="app_name" v={p.identity.app_name} />
            <KV k="env_name" v={p.identity.env_name} />
            <KV k="git_sha" v={p.identity.git_sha} />
            <KV k="broker_mode" v={p.identity.broker_mode} />
            <KV k="sidecar_version" v={p.identity.sidecar_version} />
            <KV k="mc_url_set" v={p.identity.mc_url_set} />
            <KV k="ingest_token_set" v={p.identity.ingest_token_set} />
          </Section>
        )}
        {p.seats && (
          <Section title={`Seats · count=${p.seats.count ?? "?"}`} testid={`proxied-${brain}-seats`}>
            {(p.seats.seats_held || []).length === 0 ? (
              <div className="text-[11px] text-rd-dim font-mono">none held locally</div>
            ) : (
              (p.seats.seats_held || []).map((s, i) => (
                <KV key={i} k={s.seat || "seat"} v={s.lane || "—"} testid={`seat-${i}`} />
              ))
            )}
          </Section>
        )}
        {p.heartbeat && (
          <Section title="Heartbeat" testid={`proxied-${brain}-heartbeat`}>
            <KV k="enabled" v={p.heartbeat.enabled} />
            <KV k="alive" v={p.heartbeat.alive} />
            <KV k="tick_s" v={p.heartbeat.tick_s} />
            <KV k="last_source" v={p.heartbeat.last_source} />
            <KV k="last_opinion_id" v={p.heartbeat.last_opinion_id} />
            <KV k="seconds_since" v={p.heartbeat.seconds_since_last_opinion?.toFixed?.(1) ?? p.heartbeat.seconds_since_last_opinion} />
            <KV k="last_tick_ok" v={p.heartbeat.last_tick_ok} />
            <KV k="last_tick_error" v={p.heartbeat.last_tick_error} />
          </Section>
        )}
        {p.governor_emitter && (
          <Section title="Governor emitter" testid={`proxied-${brain}-governor`}>
            <KV k="enabled" v={p.governor_emitter.enabled} />
            <KV k="alive" v={p.governor_emitter.alive} />
            <KV k="lanes_held" v={(p.governor_emitter.governor_lanes_held || []).join(", ") || "—"} />
            <KV k="last_walks" v={(p.governor_emitter.last_walks || []).length} />
          </Section>
        )}
        {p.data_keys && (
          <Section title={`Data keys · ${p.data_keys.ok ? "ok" : "degraded"}`} testid={`proxied-${brain}-datakeys`}>
            <KV k="enabled" v={p.data_keys.enabled} />
            <KV k="refresh_s" v={p.data_keys.refresh_s} />
            <KV k="whitelist" v={(p.data_keys.whitelist || []).join(", ")} />
            {(p.data_keys.env_now || []).map((k, i) => (
              <KV
                key={i}
                k={k.name}
                v={k.present ? `${k.digest?.slice?.(0, 12) || "present"}…` : "missing"}
                testid={`datakey-${k.name}`}
              />
            ))}
          </Section>
        )}
        {p.neuro_engine && (
          <Section title="Neuro engine" testid={`proxied-${brain}-neuro`}>
            <KV k="is_trained" v={p.neuro_engine.is_trained} />
            <KV k="training_data_status" v={p.neuro_engine.training_data_status} />
            <KV k="authority_class" v={p.neuro_engine.authority?.class} />
            <KV k="may_block_trade" v={p.neuro_engine.authority?.may_block_trade} />
            <KV k="may_execute" v={p.neuro_engine.authority?.may_execute} />
          </Section>
        )}
        {p.intents && (
          <Section title={`Intents · ${p.intents.total ?? "?"} total`} testid={`proxied-${brain}-intents`}>
            <KV k="last_1h" v={p.intents.last_1h} />
            <KV k="last_24h" v={p.intents.last_24h} />
            <KV k="BUY" v={p.intents.by_action?.BUY} />
            <KV k="SELL" v={p.intents.by_action?.SELL} />
            <KV k="HOLD" v={p.intents.by_action?.HOLD} />
          </Section>
        )}
      </div>
    </Card>
  );
}
