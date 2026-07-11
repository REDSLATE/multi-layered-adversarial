import React from "react";
import { Card } from "@/components/ui-bits";

/**
 * BrainProxiedStatusTile — renders the brain's status payload as
 * served by MC's in-process status endpoint at
 * `/api/admin/runtime/{brain}/status`.
 *
 * The 4 permanent brains (Camino / Barracuda / Hellcat / GTO) run
 * in-process inside MC's FastAPI event loop. The old external-
 * sidecar proxy (with its "MC could not fetch" error path and
 * force-refresh button) was REMOVED — the brains are MC. The tile
 * now only renders the success path.
 *
 * Wrapper shape:
 *   { brain, ok, ts, doctrine,
 *     _proxied_from: "in_process",
 *     _proxy_duration_ms, _proxy_age_s, _proxy_from_cache,
 *     payload }
 *
 * `payload` sections: identity · seats · heartbeat · intents ·
 *   in_process_runner. Missing sections silently no-op so partial
 *   payloads (e.g., a brain with no seats) render cleanly.
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
  if (!proxied) {
    return null;
  }

  // In-process build error — render a small honest banner. NOT a
  // call-to-action to "set BRAIN_STATUS_URL" anymore (that path is
  // dead). If the in-process build is failing, the operator should
  // check backend logs, not configure an env var.
  if (proxied.ok === false) {
    return (
      <Card testid={`brain-status-${brain}-error`} className="mb-6">
        <div className="mb-2">
          <div className="label-eyebrow">Brain telemetry · in-process</div>
          <div className="font-mono text-sm text-rd-warn flex items-center gap-2 mt-1">
            <Dot color="#F59E0B" />
            {proxied.error || "build_failed"}
          </div>
        </div>
        <div className="text-[11px] text-rd-muted font-mono leading-relaxed">
          MC could not build the in-process status payload for{" "}
          <span className="text-rd-text">{brain}</span>. Check the
          backend logs for{" "}
          <code>in_process_status_build_failed brain={brain}</code>.
        </div>
      </Card>
    );
  }

  // Degraded — Atlas read timed out on one of the sub-queries but the
  // brain itself is alive. Render an amber notice above the normal
  // sections instead of hiding everything. 2026-02-19 operator rule:
  // a slow status read must not make the operator think the brain is
  // down.
  const degradedNotice = proxied.degraded ? (
    <div
      className="border border-amber-600 bg-amber-950/20 px-3 py-2 mb-3 text-[11px] font-mono text-amber-300 flex items-start gap-2"
      data-testid={`brain-status-${brain}-degraded`}
    >
      <Dot color="#F59E0B" />
      <div>
        <div className="uppercase tracking-widest text-[10px]">
          Status read degraded
        </div>
        <div className="text-rd-muted mt-1">
          {(proxied.warnings || []).join(", ") ||
            "intent_metrics_temporarily_unavailable"}
          . Brain itself is still live — see the heartbeat card below.
        </div>
      </div>
    </div>
  ) : null;

  // Success path — render whatever sections the brain provided.
  const p = proxied.payload || {};
  const fromInProcess = proxied._proxied_from === "in_process";
  const sourceBadge = (
    <span
      className="text-[10px] font-mono uppercase tracking-widest text-emerald-400 ml-2"
      data-testid={`brain-status-${brain}-source`}
    >
      {fromInProcess ? "in-process" : "fresh"}
    </span>
  );

  return (
    <Card testid={`brain-status-proxy-${brain}`} className="mb-6">
      <div className="flex items-start justify-between mb-3">
        <div>
          <div className="label-eyebrow">Brain telemetry · in-process</div>
          <div className="font-mono text-sm mt-1 flex items-center gap-2">
            <Dot color="#10B981" />
            <span className="text-rd-text">{brain}</span>
            {sourceBadge}
          </div>
          <div className="text-[10px] text-rd-dim font-mono mt-1 truncate max-w-[50ch]">
            ↗ {proxied._proxied_from || "in_process"}
          </div>
        </div>
      </div>

      {degradedNotice}

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        {p.identity && (
          <Section title="Identity" testid={`proxied-${brain}-identity`}>
            {/* Checkin-worker eligibility chip — top-line answer to
                "is this brain even able to call MC?". Green = both
                env pairs are set AND worker was eligible at boot.
                Red = at least one env var missing; expand the rows
                below to see which. Undefined = brain hasn't shipped
                the flag yet (older sidecar). */}
            {p.identity.checkin_worker_eligible !== undefined && (
              <div
                className="flex items-center gap-2 mb-2 pb-2 border-b border-rd-border"
                data-testid={`proxied-${brain}-checkin-chip`}
              >
                <Dot
                  color={
                    p.identity.checkin_worker_eligible === true
                      ? "#10B981"
                      : p.identity.checkin_worker_eligible === false
                      ? "#DC2626"
                      : "#71717A"
                  }
                  size={10}
                />
                <span className="text-[11px] font-mono uppercase tracking-widest text-rd-text">
                  checkin worker
                </span>
                <span
                  className="text-[11px] font-mono"
                  style={{
                    color:
                      p.identity.checkin_worker_eligible === true
                        ? "#10B981"
                        : "#FCA5A5",
                  }}
                  data-testid={`proxied-${brain}-checkin-state`}
                >
                  {p.identity.checkin_worker_eligible === true
                    ? "ELIGIBLE"
                    : "NOT ELIGIBLE"}
                </span>
              </div>
            )}
            <KV k="app_name" v={p.identity.app_name} />
            <KV k="env_name" v={p.identity.env_name} />
            <KV k="git_sha" v={p.identity.git_sha} />
            <KV k="broker_mode" v={p.identity.broker_mode} />
            <KV k="sidecar_version" v={p.identity.sidecar_version} />
            {/* Check-in pair (MC ← brain periodic ping) */}
            <KV k="mc_url_set" v={p.identity.mc_url_set} testid="identity-mc-url-set" />
            <KV k="ingest_token_set" v={p.identity.ingest_token_set} testid="identity-ingest-token-set" />
            {/* Heartbeat pair (brain ← MC opinion stream) */}
            <KV k="mc_base_url_set" v={p.identity.mc_base_url_set} testid="identity-mc-base-url-set" />
            <KV
              k="heartbeat_token_set"
              v={p.identity.heartbeat_token_set}
              testid="identity-heartbeat-token-set"
            />
          </Section>
        )}
        {p.seats && (
          <Section title={`Seats · count=${p.seats.count ?? "?"}`} testid={`proxied-${brain}-seats`}>
            {(p.seats.seats_held || []).length === 0 ? (
              <div className="text-[11px] text-rd-dim font-mono">none held locally</div>
            ) : (
              (p.seats.seats_held || []).map((s, i) => (
                <KV key={`${s.seat || "seat"}-${s.lane || i}`} k={s.seat || "seat"} v={s.lane || "—"} testid={`seat-${i}`} />
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
            {(p.data_keys.env_now || []).map((k) => (
              <KV
                key={k.name}
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
        {p.write_health && (
          <WriteHealthSection brain={brain} wh={p.write_health} />
        )}
      </div>
    </Card>
  );
}

// ── Write-health card (2026-02-20 "3 clocks" doctrine) ─────────────
// Splits heartbeat freshness, decision freshness, and DB-confirmed
// intent freshness into three distinct rows so the operator can
// tell WHICH stage of the pipeline stopped when intents look stale.
//
// `wh` shape (built in `_build_in_process_status`):
//   {
//     band: "HEALTHY" | "STALE" | "DEAD" | "UNKNOWN" | "BLIND",
//     ages: { heartbeat_age_s, decision_age_s, db_write_age_s,
//             directional_write_age_s },
//     counters: { decisions_total, intent_submit_attempts_total,
//                 intent_submit_successes_total,
//                 directional_submit_successes_total,
//                 intent_submit_failures_total },
//     last_write_receipt: { intent_id, symbol, action, ingest_ts, ... }
//                       | null,
//     last_error: { msg, ts, action, symbol } | null,
//     equity_market_open: boolean,
//   }
const HEALTH_COLOR = {
  HEALTHY: "#10B981",
  STALE: "#F59E0B",
  DEAD: "#DC2626",
  UNKNOWN: "#A1A1AA",
  BLIND: "#DC2626",
};

function fmtAgeS(s) {
  if (s == null) return "—";
  const n = Math.max(0, Math.round(s));
  if (n < 90) return `${n}s`;
  if (n < 3600) return `${Math.round(n / 60)}m`;
  if (n < 86400) return `${Math.round(n / 3600)}h`;
  return `${Math.round(n / 86400)}d`;
}

function WriteHealthSection({ brain, wh }) {
  const band = wh.band || "UNKNOWN";
  const color = HEALTH_COLOR[band] || "#A1A1AA";
  const ages = wh.ages || {};
  const counters = wh.counters || {};
  const receipt = wh.last_write_receipt || null;
  const lastError = wh.last_error || null;
  return (
    <div
      className="bg-rd-bg3 border p-3"
      style={{ borderColor: color, gridColumn: "1 / -1" }}
      data-testid={`proxied-${brain}-write-health`}
    >
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-widest font-mono" style={{ color }}>
          Write health · 3 clocks
        </div>
        <span
          className="text-[10px] font-mono uppercase tracking-widest px-2 py-0.5 border"
          style={{ color, borderColor: color }}
          data-testid={`proxied-${brain}-write-health-band`}
        >
          {band}
        </span>
      </div>
      {!wh.equity_market_open && (
        <div
          className="text-[10px] font-mono text-rd-muted mb-2"
          data-testid={`proxied-${brain}-session-hint`}
        >
          equity session CLOSED — closed-session relax threshold in effect
        </div>
      )}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
        <div className="border border-rd-border p-2" data-testid={`proxied-${brain}-clock-heartbeat`}>
          <div className="text-[10px] text-rd-dim uppercase tracking-widest">Heartbeat</div>
          <div className="font-mono text-sm text-rd-text mt-1">{fmtAgeS(ages.heartbeat_age_s)}</div>
          <div className="text-[10px] text-rd-muted mt-1 font-mono">runner tick alive</div>
        </div>
        <div className="border border-rd-border p-2" data-testid={`proxied-${brain}-clock-decision`}>
          <div className="text-[10px] text-rd-dim uppercase tracking-widest">Last decision</div>
          <div className="font-mono text-sm text-rd-text mt-1">{fmtAgeS(ages.decision_age_s)}</div>
          <div className="text-[10px] text-rd-muted mt-1 font-mono">
            {counters.decisions_total ?? 0} total · {counters.intent_submit_failures_total ?? 0} fail
          </div>
        </div>
        <div className="border border-rd-border p-2" data-testid={`proxied-${brain}-clock-dbwrite`}>
          <div className="text-[10px] text-rd-dim uppercase tracking-widest">DB write (any)</div>
          <div className="font-mono text-sm text-rd-text mt-1">{fmtAgeS(ages.db_write_age_s)}</div>
          <div className="text-[10px] text-rd-muted mt-1 font-mono">
            directional · {fmtAgeS(ages.directional_write_age_s)}
          </div>
        </div>
      </div>
      <div className="mt-2 grid grid-cols-2 md:grid-cols-5 gap-2 text-[10px] font-mono text-rd-dim">
        <KV k="attempts" v={counters.intent_submit_attempts_total ?? 0} testid={`proxied-${brain}-counter-attempts`} />
        <KV k="success" v={counters.intent_submit_successes_total ?? 0} testid={`proxied-${brain}-counter-success`} />
        <KV k="directional_ok" v={counters.directional_submit_successes_total ?? 0} testid={`proxied-${brain}-counter-directional`} />
        <KV k="failures" v={counters.intent_submit_failures_total ?? 0} testid={`proxied-${brain}-counter-failures`} />
        <KV k="decisions" v={counters.decisions_total ?? 0} testid={`proxied-${brain}-counter-decisions`} />
      </div>
      {receipt && (
        <div className="mt-2 border-t border-rd-border pt-2" data-testid={`proxied-${brain}-write-receipt`}>
          <div className="text-[10px] text-rd-dim uppercase tracking-widest mb-1">Last write receipt</div>
          <div className="text-[11px] font-mono text-rd-text">
            {receipt.symbol || "—"} · {receipt.action || "—"} · {receipt.lane || "—"} · {receipt.ingest_ts || "—"}
          </div>
          {receipt.intent_id && (
            <div className="text-[10px] font-mono text-rd-muted truncate">
              intent_id={receipt.intent_id}
            </div>
          )}
        </div>
      )}
      {lastError && (
        <div
          className="mt-2 border-t border-rd-border pt-2"
          data-testid={`proxied-${brain}-write-error`}
        >
          <div className="text-[10px] uppercase tracking-widest mb-1" style={{ color: "#DC2626" }}>
            Last submit error
          </div>
          <div className="text-[11px] font-mono text-rd-danger">
            {lastError.msg || "—"}
          </div>
          <div className="text-[10px] text-rd-muted font-mono mt-1">
            {lastError.ts || "—"} · {lastError.action || "—"} · {lastError.symbol || "—"}
          </div>
        </div>
      )}
    </div>
  );
}
