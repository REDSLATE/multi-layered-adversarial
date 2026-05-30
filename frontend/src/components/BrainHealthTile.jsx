import React, { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, RUNTIME_META, relTime } from "@/lib/api";
import { Card, LoadingRow } from "@/components/ui-bits";
import { computeRegressions } from "@/lib/brainHealthAlerts";

/**
 * BrainHealthTile — single-screen confirmation that a brain is wired
 * end-to-end on prod. Reads `GET /api/admin/runtime/brain-health` (the
 * composite endpoint that joins sidecar-checkin + opinion-watchdog +
 * data-keys-audit + sovereign-audit-log per held seat × lane).
 *
 * Built for the post-redeploy verification flow (2026-02-17): instead
 * of running three curls against three different surfaces, the operator
 * glances at the four cards on this tile and sees one colored dot per
 * brain + the three sub-signals + lane-scoped seat-walk freshness.
 *
 * Color contract (doctrine-pinned thresholds returned by the API):
 *   green     — checkin fresh, opinion fresh (if seated), all held
 *               seats walked within seat_walk_max_age_s
 *   degraded  — at least one of the above is stale
 *   dead      — never checked in OR checkin > 6 × checkin_max_age_s
 *   null seat — brain isn't seated on this lane → DIMMED dot, not red
 *
 * Auto-refresh: 15s. Read-only — no mutating actions on this tile.
 */

const OVERALL_META = {
  green:    { dot: "#10B981", label: "GREEN",    text: "text-emerald-400" },
  degraded: { dot: "#F59E0B", label: "DEGRADED", text: "text-amber-400" },
  dead:     { dot: "#DC2626", label: "DEAD",     text: "text-red-500" },
};

const CHECKIN_VERDICT_META = {
  prod:         { color: "#10B981", label: "PROD" },
  preview:      { color: "#F59E0B", label: "PREVIEW" },
  policy_drift: { color: "#EAB308", label: "DRIFT" },
  invalid:      { color: "#DC2626", label: "INVALID" },
  never:        { color: "#71717A", label: "NEVER" },
};

function Dot({ color, size = 8 }) {
  return (
    <span
      className="inline-block rounded-full"
      style={{ background: color, width: size, height: size }}
    />
  );
}

function fmtAge(ageSec) {
  if (ageSec == null) return "—";
  if (ageSec < 60) return `${Math.round(ageSec)}s`;
  if (ageSec < 3600) return `${Math.round(ageSec / 60)}m`;
  if (ageSec < 86400) return `${Math.round(ageSec / 3600)}h`;
  return `${Math.round(ageSec / 86400)}d`;
}

function SignalRow({ label, value, color, testid, title }) {
  return (
    <div
      className="flex items-center justify-between text-[11px] font-mono py-1 border-b border-rd-border last:border-b-0"
      data-testid={testid}
      title={title}
    >
      <span className="text-rd-dim uppercase tracking-widest">{label}</span>
      <span className="flex items-center gap-2 text-rd-text">
        {color && <Dot color={color} />}
        <span>{value}</span>
      </span>
    </div>
  );
}

function SeatWalkGrid({ seatWalk, threshold }) {
  // Render a 4×2 grid of (role × lane) dots. Held → colored by
  // freshness; null → dimmed.
  const roles = ["strategist", "executor", "governor", "auditor"];
  const lanes = ["equity", "crypto"];
  return (
    <div className="mt-2">
      <div className="text-[9px] text-rd-dim uppercase tracking-widest mb-1">
        Seat walks (role × lane)
      </div>
      <div className="grid grid-cols-5 gap-x-2 gap-y-1 text-[10px] font-mono">
        <div></div>
        {lanes.map((l) => (
          <div key={`h-${l}`} className="text-rd-dim uppercase tracking-widest col-span-2 text-center">
            {l}
          </div>
        ))}
        {roles.map((role) => (
          <React.Fragment key={`r-${role}`}>
            <div className="text-rd-dim uppercase tracking-widest">
              {role.slice(0, 4)}
            </div>
            {lanes.map((lane) => {
              const cell = seatWalk?.[role]?.[lane];
              if (cell == null) {
                // null = brain not seated on this lane → dimmed dot
                return (
                  <div
                    key={`${role}-${lane}`}
                    className="col-span-2 flex items-center gap-1.5 opacity-30"
                    data-testid={`seat-${role}-${lane}-unseated`}
                    title={`${role} ${lane}: not seated`}
                  >
                    <Dot color="#71717A" size={6} />
                    <span className="text-rd-dim">—</span>
                  </div>
                );
              }
              const stale = cell.stale;
              const color = stale ? "#F59E0B" : "#10B981";
              return (
                <div
                  key={`${role}-${lane}`}
                  className="col-span-2 flex items-center gap-1.5"
                  data-testid={`seat-${role}-${lane}-${stale ? "stale" : "fresh"}`}
                  title={`${role} ${lane}: walked ${fmtAge(cell.age_sec)} ago${stale ? ` (>${threshold}s threshold)` : ""}`}
                >
                  <Dot color={color} size={6} />
                  <span className={stale ? "text-amber-400" : "text-emerald-400"}>
                    {fmtAge(cell.age_sec)}
                  </span>
                </div>
              );
            })}
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}

function BrainCard({ brain, payload, thresholds }) {
  const meta = RUNTIME_META[brain] || {};
  const overall = payload?.overall?.verdict || "dead";
  const overallMeta = OVERALL_META[overall] || OVERALL_META.dead;
  const reasons = payload?.overall?.reasons || [];

  // ── Per-brain worker-eligibility chip ──
  // Independent of the brain-health composite. Hits MC's status proxy
  // for this specific brain and reads `payload.identity.checkin_worker_eligible`.
  // Distinguishes three diagnostic states the composite endpoint
  // can't:  ELIGIBLE (env set + worker started) / NOT ELIGIBLE
  // (missing env var) / unknown (older sidecar / no upstream wired).
  // Cheap (proxied through MC's 10s cache). Quiet on failure — the
  // brain-health composite is the primary signal; this is an
  // upstream-truth augmentation.
  const [identity, setIdentity] = useState(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data: d } = await api.get(`/admin/runtime/${brain}/status`);
        if (cancelled) return;
        setIdentity(d?.payload?.identity || (d?.ok === false ? { _unavailable: d.error } : {}));
      } catch {
        if (!cancelled) setIdentity({ _unavailable: "fetch_failed" });
      }
    })();
    return () => { cancelled = true; };
  }, [brain]);
  const eligibility = identity?.checkin_worker_eligible;
  const eligibilityColor =
    eligibility === true ? "#10B981"
    : eligibility === false ? "#DC2626"
    : "#71717A";
  const eligibilityLabel =
    eligibility === true ? "ELIGIBLE"
    : eligibility === false ? "NOT ELIGIBLE"
    : identity?._unavailable ? "no upstream"
    : identity == null ? "…"
    : "unknown";
  // For RED state, surface which env-var pair is broken so the
  // operator doesn't have to click through to the runtime detail.
  const failedEnvVars = [];
  if (eligibility === false) {
    if (identity.mc_url_set === false) failedEnvVars.push("MC_URL");
    if (identity.ingest_token_set === false) failedEnvVars.push("INGEST_TOKEN");
    if (identity.mc_base_url_set === false) failedEnvVars.push("MC_BASE_URL");
    if (identity.heartbeat_token_set === false) failedEnvVars.push("HEARTBEAT_TOKEN");
  }

  const checkin = payload?.checkin || {};
  const checkinMeta = CHECKIN_VERDICT_META[checkin.verdict] || CHECKIN_VERDICT_META.never;

  const opinion = payload?.opinion || {};
  const opinionFresh = !opinion.silent && opinion.age_sec != null;
  // Doctrine pin (2026-02-17): executor / crypto seats DO NOT post
  // opinions — they execute strategist signals. Showing "OPINION: NEVER"
  // for an executor-seated brain (e.g. Alpha) is misleading the operator
  // into thinking it's a bug. Detect executor-only seating and surface
  // "n/a (executor)" with a neutral dot.
  const seatWalk = payload?.seat_walk || {};
  const heldSeats = [];
  Object.entries(seatWalk).forEach(([role, lanes]) => {
    Object.entries(lanes || {}).forEach(([lane, cell]) => {
      if (cell != null) heldSeats.push({ role, lane });
    });
  });
  const heldOpinionProducingSeats = heldSeats.filter(
    (s) => s.role !== "executor",
  );
  const executorOnly =
    heldSeats.length > 0 && heldOpinionProducingSeats.length === 0;

  const opinionColor = executorOnly
    ? "#71717A"  // dim — doesn't apply, not a problem
    : opinion.age_sec == null
    ? "#71717A"
    : opinion.silent
    ? "#F59E0B"
    : "#10B981";
  const opinionValue = executorOnly
    ? "n/a (executor)"
    : `${opinionFresh ? "fresh" : opinion.age_sec == null ? "never" : "silent"} · ${fmtAge(opinion.age_sec)}`;
  const opinionTitle = executorOnly
    ? "Executor seats route orders; they do not post opinions. This row is informational only."
    : opinion.last_posted_at
    ? `Last opinion: ${opinion.last_posted_at}`
    : "No opinions on record";

  const dataKeys = payload?.data_keys || {};
  const dataKeysColor = dataKeys.last_fetch_ts ? "#10B981" : "#71717A";

  return (
    <Link
      to={`/admin/runtime/${brain}`}
      className="block bg-rd-bg border border-rd-border p-4 transition-colors hover:bg-[#141414] hover:border-rd-warn focus:outline-none focus:border-rd-warn focus:bg-[#141414] cursor-pointer no-underline"
      style={{ borderLeft: `3px solid ${meta.color || "#71717A"}` }}
      data-testid={`brain-health-card-${brain}`}
      title={`Open ${meta.label || brain.toUpperCase()} runtime detail`}
    >
      <div className="flex items-start justify-between mb-3">
        <div>
          <div className="flex items-center gap-2">
            <span
              className="font-display font-bold text-sm tracking-tight"
              style={{ color: meta.color }}
            >
              {meta.label || brain.toUpperCase()}
            </span>
            <Dot color={overallMeta.dot} size={8} />
            <span className={`text-[10px] font-mono uppercase tracking-widest ${overallMeta.text}`}>
              {overallMeta.label}
            </span>
          </div>
          <div className="text-[9px] text-rd-dim uppercase tracking-widest mt-1">
            {meta.project || ""}
          </div>
        </div>
        <span
          className="text-[10px] font-mono text-rd-dim uppercase tracking-widest opacity-60 group-hover:opacity-100"
          aria-hidden="true"
        >
          ↗
        </span>
      </div>

      <SignalRow
        label="Worker"
        value={
          failedEnvVars.length > 0
            ? `${eligibilityLabel} · missing: ${failedEnvVars.join(", ")}`
            : eligibilityLabel
        }
        color={eligibilityColor}
        testid={`brain-health-${brain}-worker-eligibility`}
        title={
          eligibility === true
            ? "Brain's check-in worker reports ELIGIBLE at boot (both env-var pairs set)"
            : eligibility === false
            ? `Brain's check-in worker did NOT start. Missing env: ${failedEnvVars.join(", ") || "see runtime detail"}`
            : identity?._unavailable
            ? `MC has no proxy upstream wired for ${brain.toUpperCase()} — set ${brain.toUpperCase()}_STATUS_URL`
            : "Brain has not shipped the checkin_worker_eligible boolean yet"
        }
      />
      <SignalRow
        label="Checkin"
        value={`${checkinMeta.label} · ${fmtAge(checkin.age_sec)}`}
        color={checkinMeta.color}
        testid={`brain-health-${brain}-checkin`}
        title={checkin.last_checkin_at ? `Last: ${checkin.last_checkin_at}` : "Never"}
      />
      <SignalRow
        label="Opinion"
        value={opinionValue}
        color={opinionColor}
        testid={`brain-health-${brain}-opinion`}
        title={opinionTitle}
      />
      <SignalRow
        label="Data keys"
        value={`${dataKeys.last_fetch_ts ? `${(dataKeys.served_fields || []).length} field(s)` : "none"} · ${fmtAge(dataKeys.age_sec)}`}
        color={dataKeysColor}
        testid={`brain-health-${brain}-datakeys`}
        title={`24h fetches: ${dataKeys.fetch_count_24h || 0}`}
      />

      <SeatWalkGrid seatWalk={payload?.seat_walk} threshold={thresholds?.seat_walk_max_age_s} />

      {reasons.length > 0 && (
        <div
          className="mt-3 pt-2 border-t border-rd-border"
          data-testid={`brain-health-${brain}-reasons`}
        >
          <div className="text-[9px] text-rd-dim uppercase tracking-widest mb-1">
            Why
          </div>
          <div className="flex flex-wrap gap-1">
            {reasons.map((r) => (
              <span
                key={r}
                className="text-[9px] font-mono px-1.5 py-0.5 border border-rd-border text-amber-300"
              >
                {r}
              </span>
            ))}
          </div>
        </div>
      )}
    </Link>
  );
}

export default function BrainHealthTile() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [lastFetched, setLastFetched] = useState(null);

  // ── Regression alerting (operator opt-in) ──
  // Persisted in localStorage so the operator's preference survives
  // refreshes. Default OFF — desktop notifications require explicit
  // user consent both at the OS level AND by intent (per RedEye
  // author's doctrine pin, no surprise pings).
  const [alertsEnabled, setAlertsEnabled] = useState(() => {
    try {
      return localStorage.getItem("brain_health_alerts_enabled") === "true";
    } catch {
      return false;
    }
  });
  const [notifPerm, setNotifPerm] = useState(() => {
    if (typeof Notification === "undefined") return "unsupported";
    return Notification.permission; // "granted" | "denied" | "default"
  });
  // Mutable refs to avoid re-renders for in-flight bookkeeping.
  const prevVerdictsRef = useRef({});
  const lastNotifMsRef = useRef({});

  const load = useCallback(async () => {
    try {
      const { data: d } = await api.get("/admin/runtime/brain-health");
      setData(d);
      setLastFetched(new Date());
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  // ── Regression-detection side effect ──
  // Runs whenever the fleet payload updates. Doctrine pins enforced
  // by the pure `computeRegressions(...)` helper:
  //   - Only green → degraded/dead fires (not the inverse, not
  //     degraded↔dead flips).
  //   - Per-brain 60s debounce against pod flapping.
  //   - First-load (no prior verdict) NEVER fires.
  useEffect(() => {
    if (!data?.brains) return;
    const currentVerdicts = {};
    for (const [brain, payload] of Object.entries(data.brains)) {
      currentVerdicts[brain] = payload?.overall?.verdict || "dead";
    }
    if (alertsEnabled && notifPerm === "granted") {
      const nowMs = Date.now();
      const regressed = computeRegressions(
        prevVerdictsRef.current,
        currentVerdicts,
        lastNotifMsRef.current,
        nowMs,
      );
      for (const brain of regressed) {
        const verdict = currentVerdicts[brain];
        const reasons = data.brains[brain]?.overall?.reasons || [];
        const meta = RUNTIME_META[brain] || {};
        try {
          new Notification(`${meta.label || brain.toUpperCase()} ${verdict.toUpperCase()}`, {
            body: reasons.length ? reasons.slice(0, 3).join(" · ") : "Brain health regressed.",
            tag: `brain-health-${brain}`,
            requireInteraction: verdict === "dead",
          });
        } catch (e) {
          // Some browsers throw if the page isn't focused; that's fine —
          // the notification permission is still effective at the OS
          // level, and the dropped alert isn't worth retrying.
          // eslint-disable-next-line no-console
          console.warn("[brain-health] notification dispatch failed:", e?.message);
        }
        lastNotifMsRef.current[brain] = nowMs;
      }
    }
    // ALWAYS update prev verdicts — even when alerts are disabled —
    // so flipping alerts on mid-session doesn't fire a phantom alert
    // for a brain that was already degraded.
    prevVerdictsRef.current = currentVerdicts;
  }, [data, alertsEnabled, notifPerm]);

  const handleToggleAlerts = useCallback(async () => {
    if (alertsEnabled) {
      setAlertsEnabled(false);
      try {
        localStorage.setItem("brain_health_alerts_enabled", "false");
      } catch {
        // localStorage can be denied; toggle still takes effect for this session.
      }
      return;
    }
    if (typeof Notification === "undefined") {
      setNotifPerm("unsupported");
      return;
    }
    let perm = Notification.permission;
    if (perm === "default") {
      try {
        perm = await Notification.requestPermission();
      } catch {
        perm = "denied";
      }
    }
    setNotifPerm(perm);
    if (perm === "granted") {
      setAlertsEnabled(true);
      try {
        localStorage.setItem("brain_health_alerts_enabled", "true");
      } catch {
        // ignore — preference will reset on next load
      }
    }
  }, [alertsEnabled]);

  const thresholds = data?.thresholds;
  const brains = data?.brains || {};
  const brainKeys = Object.keys(brains);

  return (
    <Card className="p-0 overflow-hidden" testid="brain-health-tile">
      <div className="border-b border-rd-border p-4 flex items-start justify-between">
        <div>
          <div className="label-eyebrow mb-1">
            Operator · single-glance fleet readiness
          </div>
          <h2 className="font-display text-xl font-bold tracking-tight">
            Brain Health
          </h2>
          <p className="text-[11px] text-rd-muted mt-1 font-mono leading-relaxed max-w-3xl">
            Composite of sidecar check-in · opinion freshness · market-data
            keys fetch · per-seat × per-lane sovereign-audit walk.
            Thresholds doctrine-pinned: checkin&nbsp;
            <span className="text-rd-text">≤{thresholds?.checkin_max_age_s ?? "?"}s</span>,
            opinion&nbsp;
            <span className="text-rd-text">≤{thresholds?.opinion_max_age_s ?? "?"}s</span>,
            seat-walk&nbsp;
            <span className="text-rd-text">≤{thresholds?.seat_walk_max_age_s ?? "?"}s</span>.
            Null cells = brain not seated on that lane (dimmed dot, not red).
          </p>
        </div>
        <div className="text-right">
          <div className="text-[9px] text-rd-dim uppercase tracking-widest">
            Last refresh
          </div>
          <div className="text-[10px] font-mono text-rd-text mt-0.5">
            {lastFetched ? relTime(lastFetched.toISOString()) : "—"}
          </div>
          <div className="flex items-center gap-2 mt-2 justify-end">
            <button
              onClick={handleToggleAlerts}
              className={`text-[10px] font-mono uppercase tracking-widest border px-2 py-1 transition-colors ${
                alertsEnabled
                  ? "border-emerald-500 text-emerald-400 hover:text-emerald-300"
                  : "border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-warn"
              }`}
              data-testid="brain-health-alerts-toggle"
              title={
                notifPerm === "unsupported"
                  ? "Browser does not support desktop notifications"
                  : notifPerm === "denied"
                  ? "Notifications blocked at OS/browser level — re-enable in site settings"
                  : alertsEnabled
                  ? "Click to disable. Pings ONLY on green→degraded/dead, debounced 60s/brain."
                  : "Click to enable desktop notifications on green→degraded/dead regressions"
              }
              disabled={notifPerm === "unsupported"}
            >
              {alertsEnabled ? "● alerts ON" : "○ alerts OFF"}
            </button>
            <button
              onClick={load}
              className="text-[10px] font-mono uppercase tracking-widest text-rd-warn hover:text-rd-text border border-rd-border px-2 py-1"
              data-testid="brain-health-refresh-btn"
            >
              ↻ refresh
            </button>
          </div>
          {notifPerm === "denied" && alertsEnabled === false && (
            <div className="text-[9px] text-amber-400 font-mono mt-1" data-testid="brain-health-alerts-denied">
              browser blocked notifications
            </div>
          )}
        </div>
      </div>

      {err && (
        <div className="px-4 py-3 text-xs text-red-400 font-mono border-b border-rd-border" data-testid="brain-health-error">
          {err}
        </div>
      )}

      <div className="p-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
        {!data && <LoadingRow />}
        {brainKeys.map((brain) => (
          <BrainCard
            key={brain}
            brain={brain}
            payload={brains[brain]}
            thresholds={thresholds}
          />
        ))}
      </div>
    </Card>
  );
}
