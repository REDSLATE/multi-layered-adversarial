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
  alpha: {
    label: "ALPHA",
    sub: "Decider seat. Strategist voice — bullish/long-direction lead.",
    color: "#3B82F6",
    expected_seats: ["decider", "executor"],
    test_intent: { action: "BUY", symbol: "SPY", lane: "equity", confidence: 0.55,
                   rationale: "operator wiring test — alpha" },
  },
  camaro: {
    label: "CAMARO",
    sub: "Decider / Executor. Posts intents that route through the gate chain.",
    color: "#F59E0B",
    expected_seats: ["decider", "executor"],
    test_intent: { action: "BUY", symbol: "AAPL", lane: "equity", confidence: 0.55,
                   rationale: "operator test ping" },
  },
  chevelle: {
    label: "CHEVELLE",
    sub: "Governor. Veto-only. No trades originated — observes & validates.",
    color: "#3B82F6",
    expected_seats: ["governor"],
    test_intent: null,  // governor doesn't trade
  },
  redeye: {
    label: "REDEYE",
    sub: "Advisor / Opponent. Bearish scout. Cannot execute.",
    color: "#DC2626",
    expected_seats: ["advisor", "opponent"],
    test_intent: null,
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
  const [testResult, setTestResult] = useState(null);
  const [testSubmitting, setTestSubmitting] = useState(false);

  const refresh = useCallback(async () => {
    if (!profile) return;
    const [d, h, s, r] = await Promise.all([
      api.get("/admin/diagnostics"),
      api.get(`/admin/intents/honesty?stack=${brain}&hours=24`),
      api.get(`/admin/sovereign/state/${brain}`).catch(() => ({ data: null })),
      api.get("/admin/roster").catch(() => ({ data: null })),
    ]);
    setDiag(d.data);
    setHonesty(h.data);
    setSovereign(s.data);
    setRoster(r.data);
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
              const seatHere = roster?.assignments
                ? Object.entries(roster.assignments).find(([, holder]) => holder === brain)?.[0]
                : null;
              const expected = profile.expected_seats;
              const seatOk = seatHere && expected.includes(seatHere);
              return (
                <dl className="space-y-2 text-sm">
                  <Row k="Current seat" v={seatHere || "—"} warn={!seatOk} />
                  <Row k="Expected seats" v={expected.join(" or ")} />
                  <Row k="Authority" v={sovereign?.authority_state || sovereign?.posted_as || "—"} />
                  <Row k="may_decide" v={String(sovereign?.may_decide ?? "—")} />
                  <Row k="may_execute" v={String(sovereign?.may_execute ?? "—")} />
                  <Row k="may_veto" v={String(sovereign?.may_veto ?? "—")} />
                  <Row k="Seat epoch" v={roster?.seat_epoch ?? "—"} />
                  {seatHere && !seatOk && (
                    <p className="mt-3 rounded border border-amber-700/50 bg-amber-950/30 p-2 text-xs text-amber-300"
                       data-testid={`brain-seat-mismatch-${brain}`}>
                      ⚠️ Seat mismatch — currently <code>{seatHere}</code>, expected one of{" "}
                      <code>{expected.join(", ")}</code>. Rotate via Roster page.
                    </p>
                  )}
                  {!seatHere && (
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
