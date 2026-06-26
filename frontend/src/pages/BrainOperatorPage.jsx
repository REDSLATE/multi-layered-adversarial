import React, { useCallback, useEffect, useState } from "react";
import { useParams, Navigate } from "react-router-dom";
import { api } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState, LoadingRow } from "@/components/ui-bits";

/**
 * BrainOperatorPage — verification dashboard for a single brain.
 *
 * One route per brain: /admin/camaro, /admin/chevelle, /admin/redeye.
 * Operator uses it to verify the brain after a runtime patch:
 *   - Is it heartbeating?
 *   - Are intents flowing AND tagged with honesty telemetry?
 *   - Are blocked-trades-as-HOLD being surfaced?
 *   - Can MC reach it (POST test intent → see receipt)?
 */

const BRAIN_PROFILE = {
  camino: {
    label: "CAMINO",
    sub: "Strategist voice. Eligible for every seat.",
    color: "#3B82F6",
    expected_seats: ["strategist", "executor", "auditor", "governor",
                     "opponent", "advisor", "crypto"],
    test_intent: { action: "BUY", symbol: "SPY", lane: "equity", confidence: 0.55,
                   rationale: "operator wiring test — alpha" },
  },
  barracuda: {
    label: "BARRACUDA",
    sub: "Eligible for every seat. Posts intents through the gate chain.",
    color: "#F59E0B",
    expected_seats: ["strategist", "executor", "auditor", "governor",
                     "opponent", "advisor", "crypto"],
    test_intent: { action: "BUY", symbol: "AAPL", lane: "equity", confidence: 0.55,
                   rationale: "operator test ping" },
  },
  hellcat: {
    label: "HELLCAT",
    sub: "Eligible for every seat. Default governor; can specialize elsewhere.",
    color: "#3B82F6",
    expected_seats: ["strategist", "executor", "auditor", "governor",
                     "opponent", "advisor", "crypto"],
    test_intent: { action: "BUY", symbol: "SPY", lane: "equity", confidence: 0.55,
                   rationale: "operator wiring test — chevelle" },
  },
  gto: {
    label: "GTO",
    sub: "Adversarial voice. Eligible for every seat. Vacant by default.",
    color: "#DC2626",
    expected_seats: ["strategist", "executor", "auditor", "governor",
                     "opponent", "advisor", "crypto"],
    test_intent: { action: "SELL", symbol: "SPY", lane: "equity", confidence: 0.55,
                   rationale: "operator wiring test — redeye (bearish)" },
  },
};

const STALE_THRESHOLD_S = 90;
const FREEZE_THRESHOLD_S = 300;  // 5 min = suspicious freeze

function fmtAge(seconds) {
  if (seconds == null) return "never";
  if (seconds < 60) return `${seconds.toFixed(0)}s ago`;
  if (seconds < 3600) return `${(seconds / 60).toFixed(1)}m ago`;
  return `${(seconds / 3600).toFixed(1)}h ago`;
}

function healthBadge(ageSec) {
  if (ageSec == null) return { text: "NEVER", color: "#6B7280" };
  if (ageSec < STALE_THRESHOLD_S) return { text: "LIVE", color: "#10B981" };
  if (ageSec < FREEZE_THRESHOLD_S) return { text: "STALE", color: "#F59E0B" };
  return { text: "FROZEN", color: "#DC2626" };
}

export default function BrainOperatorPage() {
  const { brain } = useParams();
  const profile = BRAIN_PROFILE[brain];

  const [diag, setDiag] = useState(null);
  const [intents, setIntents] = useState(null);
  const [honesty, setHonesty] = useState(null);
  const [sovereign, setSovereign] = useState(null);
  const [roster, setRoster] = useState(null);
  const [laneReadiness, setLaneReadiness] = useState({ equity: null, crypto: null });
  const [testResult, setTestResult] = useState(null);
  const [testSubmitting, setTestSubmitting] = useState(false);

  const refresh = useCallback(async () => {
    if (!profile) return;
    const [d, h, s, r, eqLane, crLane] = await Promise.all([
      api.get("/admin/diagnostics"),
      api.get(`/admin/intents/honesty?stack=${brain}&hours=24`),
      api.get(`/admin/sovereign/state/${brain}`).catch(() => ({ data: null })),
      api.get("/admin/roster").catch(() => ({ data: null })),
      api.get("/admin/lane-readiness/equity?hours=24").catch(() => ({ data: null })),
      api.get("/admin/lane-readiness/crypto?hours=24").catch(() => ({ data: null })),
    ]);
    setDiag(d.data);
    setHonesty(h.data);
    setSovereign(s.data);
    setRoster(r.data);
    setLaneReadiness({ equity: eqLane.data, crypto: crLane.data });
    try {
      const rt = await api.get(`/runtime/${brain}/status`);
      setIntents(rt.data);
    } catch {
      setIntents({});
    }
  }, [brain, profile]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 15_000);  // 15s polling
    return () => clearInterval(id);
  }, [refresh]);

  if (!profile) {
    return <Navigate to="/admin/overview" replace />;
  }

  const runtimeRow = diag?.runtimes?.find((r) => r.runtime === brain);
  const hbAge = runtimeRow?.heartbeat_age_seconds;
  const hbState = healthBadge(hbAge);

  const postTestIntent = async () => {
    if (!profile.test_intent) return;
    setTestSubmitting(true);
    setTestResult(null);
    try {
      const r = await api.post("/admin/intents", {
        stack: brain,
        ...profile.test_intent,
      });
      setTestResult({ ok: true, data: r.data });
    } catch (e) {
      setTestResult({ ok: false, error: e?.response?.data?.detail || e.message });
    } finally {
      setTestSubmitting(false);
    }
  };

  return (
    <div className="reveal" data-testid={`brain-operator-${brain}`}>
      <PageHeader
        eyebrow={`Brain · ${profile.label}`}
        title={`${profile.label} — Operator`}
        sub={profile.sub}
        right={
          <div className="flex items-center gap-2">
            <Badge color={hbState.color} data-testid={`brain-hb-badge-${brain}`}>{hbState.text}</Badge>
            <Badge color={profile.color}>{profile.label}</Badge>
          </div>
        }
        testid={`brain-header-${brain}`}
      />

      {!diag && <LoadingRow />}

      {diag && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-4">
          {/* Heartbeat / freeze detector */}
          <Card title="Liveness" data-testid={`brain-card-liveness-${brain}`}>
            <dl className="space-y-2 text-sm">
              <Row k="Heartbeat" v={fmtAge(hbAge)} />
              <Row k="Last seen" v={runtimeRow?.heartbeat?.last_seen || "never"} />
              <Row k="Stale threshold" v={`${STALE_THRESHOLD_S}s`} />
              <Row k="Freeze threshold" v={`${FREEZE_THRESHOLD_S}s`} />
              {hbAge != null && hbAge >= FREEZE_THRESHOLD_S && (
                <p className="mt-3 rounded border border-rose-700/50 bg-rose-950/30 p-2 text-xs text-rose-300"
                   data-testid={`brain-freeze-warning-${brain}`}>
                  ⚠️ Sidecar likely frozen — same pattern as the httpx keep-alive bug.
                  Restart the runtime process and check logs around <code>{runtimeRow?.heartbeat?.last_seen}</code>.
                </p>
              )}
            </dl>
          </Card>

          {/* Seat & Authority — MC's current view of where this brain sits */}
          <Card title="Seat & Authority" data-testid={`brain-card-seat-${brain}`}>
            {(() => {
              const seatsHere = roster?.assignments
                ? Object.entries(roster.assignments)
                    .filter(([, holder]) => holder === brain)
                    .map(([seat]) => seat)
                : [];
              const expected = profile.expected_seats;
              const unexpectedSeats = seatsHere.filter((s) => !expected.includes(s));
              const seatOk = seatsHere.length > 0 && unexpectedSeats.length === 0;
              return (
                <dl className="space-y-2 text-sm">
                  <Row
                    k={`Current seat${seatsHere.length === 1 ? "" : "s"}`}
                    v={
                      seatsHere.length > 0 ? (
                        <span
                          className="font-mono"
                          data-testid={`brain-current-seats-${brain}`}
                        >
                          {seatsHere.join(", ")}
                        </span>
                      ) : (
                        "—"
                      )
                    }
                    warn={!seatOk}
                  />
                  <Row k="Eligible seats" v={expected.join(" or ")} />
                  <Row k="Authority" v={sovereign?.authority_state || sovereign?.posted_as || "—"} />
                  <Row k="may_decide" v={String(sovereign?.may_decide ?? "—")} />
                  <Row k="may_execute" v={String(sovereign?.may_execute ?? "—")} />
                  <Row k="may_veto" v={String(sovereign?.may_veto ?? "—")} />
                  <Row k="Seat epoch" v={roster?.seat_epoch ?? "—"} />
                  {unexpectedSeats.length > 0 && (
                    <p className="mt-3 rounded border border-amber-700/50 bg-amber-950/30 p-2 text-xs text-amber-300"
                       data-testid={`brain-seat-mismatch-${brain}`}>
                      ⚠️ Unexpected seat assignment — <code>{unexpectedSeats.join(", ")}</code>.
                      Eligible: <code>{expected.join(", ")}</code>.
                    </p>
                  )}
                  {seatsHere.length === 0 && (
                    <p className="mt-3 rounded border border-rose-700/50 bg-rose-950/30 p-2 text-xs text-rose-300"
                       data-testid={`brain-no-seat-${brain}`}>
                      ⚠️ No seat assigned. This brain holds no chair on the council.
                    </p>
                  )}
                </dl>
              );
            })()}
          </Card>

          {/* Honesty audit */}
          <Card title="Honesty (24h)" data-testid={`brain-card-honesty-${brain}`}>
            <dl className="space-y-2 text-sm">
              <Row k="Total intents" v={honesty?.total_intents_in_window ?? "—"} />
              <Row k="Would-have-traded" v={honesty?.blocked_directional ?? 0} />
              <Row k="Blocked %" v={`${honesty?.blocked_pct_of_total ?? 0}%`}
                   warn={honesty?.blocked_pct_of_total > 30} />
            </dl>
            {honesty?.by_reason && Object.keys(honesty.by_reason).length > 0 && (
              <div className="mt-3 space-y-1 text-xs text-zinc-400">
                <div className="font-mono uppercase tracking-wider text-zinc-500">Hold reasons</div>
                {Object.entries(honesty.by_reason).map(([reason, count]) => (
                  <div key={reason} className="flex justify-between border-b border-zinc-800 py-1">
                    <span>{reason}</span>
                    <span className="text-zinc-200">{count}</span>
                  </div>
                ))}
              </div>
            )}
          </Card>

          {/* Test intent posting */}
          <Card title="Verify Wiring" data-testid={`brain-card-test-${brain}`}>
            {profile.test_intent ? (
              <>
                <p className="text-xs text-zinc-400 mb-3">
                  Posts a synthetic intent via the admin proxy. If MC's gate chain
                  accepts it, the wiring is alive end-to-end.
                </p>
                <button
                  type="button"
                  data-testid={`brain-test-post-${brain}`}
                  onClick={postTestIntent}
                  disabled={testSubmitting}
                  className="rounded bg-amber-600 px-4 py-2 text-sm font-medium text-zinc-900 hover:bg-amber-500 disabled:opacity-50"
                >
                  {testSubmitting ? "Submitting…" : `POST TEST INTENT (${profile.test_intent.action} ${profile.test_intent.symbol})`}
                </button>
                {testResult && (
                  <pre data-testid={`brain-test-result-${brain}`}
                       className={`mt-3 rounded p-2 text-xs ${
                         testResult.ok
                           ? "border border-emerald-700/50 bg-emerald-950/30 text-emerald-300"
                           : "border border-rose-700/50 bg-rose-950/30 text-rose-300"
                       }`}>
                    {JSON.stringify(testResult, null, 2)}
                  </pre>
                )}
              </>
            ) : (
              <EmptyState
                title={`${profile.label} doesn't trade`}
                sub={`Seat is ${profile.expected_seats.join("/")} — no executable test intent. Verify via heartbeat + honesty above.`}
              />
            )}
          </Card>
        </div>
      )}

      {/* Per-lane activity (24h) — both equity and crypto for this brain */}
      {(laneReadiness.equity || laneReadiness.crypto) && (
        <Card title="Per-lane activity (24h)" className="mt-6"
              data-testid={`brain-card-lane-activity-${brain}`}>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {["equity", "crypto"].map((lane) => {
              const lr = laneReadiness[lane];
              const brainCadence = lr?.emission_cadence?.by_brain?.[brain];
              const states = brainCadence?.states || {};
              const ready = lr?.ready_to_trade;
              const checks = lr?.checks || {};
              const firstBlocker = Object.entries(checks).find(([, c]) => !c?.ok);
              return (
                <div
                  key={lane}
                  data-testid={`brain-lane-${lane}-${brain}`}
                  className="rounded border border-zinc-800 bg-zinc-900/40 p-3"
                >
                  <div className="flex items-baseline justify-between">
                    <span className="font-mono text-xs uppercase tracking-wider text-zinc-400">
                      {lane}
                    </span>
                    <Badge color={ready ? "#10B981" : "#DC2626"}>
                      {ready ? "READY" : "BLOCKED"}
                    </Badge>
                  </div>
                  <dl className="mt-3 space-y-1 text-xs">
                    <Row k="Emissions (24h)" v={brainCadence?.total ?? 0} />
                    <Row k="dry_run_passed" v={states.dry_run_passed ?? 0} />
                    <Row k="dry_run_blocked" v={states.dry_run_blocked ?? 0} />
                    <Row k="pending" v={states.pending ?? 0} />
                    <Row k="Executed" v={lr?.emission_cadence?.executed ?? 0} />
                    <Row k="Last emit" v={brainCadence?.latest?.slice(11, 19) || "—"} />
                  </dl>
                  {!ready && firstBlocker && (
                    <p className="mt-3 rounded border border-rose-700/50 bg-rose-950/30 p-2 text-xs text-rose-300">
                      <span className="font-mono uppercase text-rose-400">
                        {firstBlocker[0]}
                      </span>{" "}
                      — {firstBlocker[1].detail}
                      {firstBlocker[1].fix && (
                        <div className="mt-1 text-amber-300">
                          Fix: <code>{firstBlocker[1].fix}</code>
                        </div>
                      )}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        </Card>
      )}

      {/* Recent intents with honesty telemetry */}
      {honesty?.items && honesty.items.length > 0 && (
        <Card title="Blocked but directional (24h)" className="mt-6"
              data-testid={`brain-card-blocked-${brain}`}>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-zinc-800 text-left text-zinc-500">
                  <th className="py-2 px-2">Time</th>
                  <th className="px-2">Symbol</th>
                  <th className="px-2">Market</th>
                  <th className="px-2">Display</th>
                  <th className="px-2">Raw conf</th>
                  <th className="px-2">Final conf</th>
                  <th className="px-2">Hold reason</th>
                </tr>
              </thead>
              <tbody>
                {honesty.items.slice(0, 20).map((i) => (
                  <tr key={i.intent_id} className="border-b border-zinc-900 hover:bg-zinc-900/40">
                    <td className="py-2 px-2 font-mono text-zinc-500">{i.ingest_ts?.slice(11, 19)}</td>
                    <td className="px-2 font-mono text-amber-400">{i.symbol}</td>
                    <td className="px-2 text-emerald-400">{i.market_decision || "—"}</td>
                    <td className="px-2 text-zinc-400">{i.display_action || i.action}</td>
                    <td className="px-2 font-mono">{i.raw_confidence?.toFixed(2) ?? "—"}</td>
                    <td className="px-2 font-mono">{i.confidence?.toFixed(2) ?? "—"}</td>
                    <td className="px-2 text-zinc-300">{i.hold_reason || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {/* Runtime detail link */}
      <div className="mt-6 text-xs text-zinc-500">
        Need deeper telemetry? See the{" "}
        <a href={`/admin/runtime/${brain}`} className="underline hover:text-zinc-300">
          full runtime detail page
        </a>
        .
      </div>
    </div>
  );
}

function Row({ k, v, warn }) {
  return (
    <div className="flex items-baseline justify-between border-b border-zinc-800 py-1">
      <dt className="font-mono text-xs uppercase tracking-wider text-zinc-500">{k}</dt>
      <dd className={`font-mono text-sm ${warn ? "text-rose-300" : "text-zinc-200"}`}>{v}</dd>
    </div>
  );
}
