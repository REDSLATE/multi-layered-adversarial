import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";
import { ArrowsClockwise, Warning, CheckCircle, Lightning, ShieldSlash } from "@phosphor-icons/react";
import ConvictionFloorKnob from "@/components/ConvictionFloorKnob";
import GateFailureDigest from "@/components/GateFailureDigest";
import KrakenPairEditor from "@/components/KrakenPairEditor";
import WatchlistPanel from "@/components/WatchlistPanel";
import TapeQualityPanel from "@/components/TapeQualityPanel";
import SellPointPanel from "@/components/SellPointPanel";
import EntryModePanel from "@/components/EntryModePanel";
import GateProgressBar from "@/components/GateProgressBar";
import ForensicsPanel from "@/components/ForensicsPanel";
import OutcomeEnginePanel from "@/components/OutcomeEnginePanel";
import ExitMonitorPanel from "@/components/ExitMonitorPanel";
import ExpectancyPanel from "@/components/ExpectancyPanel";
import GainGoalPanel from "@/components/GainGoalPanel";
import ScannerPanel from "@/components/ScannerPanel";
import DailyBudgetTile from "@/components/DailyBudgetTile";
import OpportunityPolicyPanel from "@/components/OpportunityPolicyPanel";
import OptionsPanel from "@/components/OptionsPanel";
import OutcomePipelinePanel from "@/components/OutcomePipelinePanel";

/**
 * Operator Control — one-glance status + one-click toggles for the
 * two switches that gate live trading:
 *
 *   1. Arbiter runtime_mode  (DISARMED ↔ LIVE)
 *       → controls whether pulse-emitted arbitrations turn into intents
 *   2. Master switch         (trading_controls.enabled)
 *       → controls whether auto_router routes intents to brokers
 *
 * PLUS: the last N pulse ticks with `arbitrations / intents_emitted /
 * runtime_mode` inline — the single readout that makes it obvious at
 * a glance whether the pulse → arbiter → intent loop is closed.
 * Non-zero `intents` for a stretch of ticks = loop is firing.
 * Zero `intents` while `brains_completed=4/4` = arbiter is DISARMED
 * OR every seat resolved "all_flat" (no consensus).
 */
function relTime(iso) {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function Toggle({ label, checked, busy, onToggle, colorOn = "#10B981", colorOff = "#71717A", testid }) {
  const trackColor = checked ? colorOn : colorOff;
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={busy}
      onClick={onToggle}
      data-testid={testid}
      className="flex items-center gap-3 py-1.5 group disabled:opacity-50 disabled:cursor-not-allowed"
    >
      <span className="text-[10px] uppercase tracking-widest text-rd-dim font-mono">
        {label}
      </span>
      <span
        className="relative inline-block w-10 h-5 rounded-full transition-colors"
        style={{ backgroundColor: trackColor }}
      >
        <span
          className="absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform"
          style={{ transform: checked ? "translateX(20px)" : "translateX(0)" }}
        />
      </span>
      <span
        className="text-xs font-mono font-bold uppercase tracking-widest"
        style={{ color: trackColor }}
      >
        {busy ? "…" : (checked ? "ON" : "OFF")}
      </span>
    </button>
  );
}

export default function OperatorControl() {
  const [arbiter, setArbiter] = useState(null);      // { runtime_mode: "LIVE" | "DISARMED" }
  const [tradingCtl, setTradingCtl] = useState(null); // /admin/trading/status
  const [ticks, setTicks] = useState([]);
  const [roster, setRoster] = useState(null);        // /admin/roster
  const [router, setRouter] = useState(null);        // /admin/auto-router/status
  const [busyArbiter, setBusyArbiter] = useState(false);
  const [busyMaster, setBusyMaster] = useState(false);
  const [busyRefresh, setBusyRefresh] = useState(false);
  const [busyForceTick, setBusyForceTick] = useState(false);
  const [busyProbe, setBusyProbe] = useState(false);
  const [probe, setProbe] = useState(null);
  const [retention, setRetention] = useState(null); // /admin/retention/status
  const [busyPurge, setBusyPurge] = useState(false);
  const [purgeResult, setPurgeResult] = useState(null);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    setBusyRefresh(true);
    try {
      const [a, t, k, r, ro, re] = await Promise.all([
        api.get("/mc/arbiter/state").catch((e) => ({ data: { _error: e?.response?.data?.detail || e.message } })),
        api.get("/admin/trading/status").catch((e) => ({ data: { _error: e?.response?.data?.detail || e.message } })),
        api.get("/mc/pulse-health/ticks?limit=15").catch((e) => ({ data: { _error: e?.response?.data?.detail || e.message } })),
        api.get("/admin/roster").catch((e) => ({ data: { _error: e?.response?.data?.detail || e.message } })),
        api.get("/admin/auto-router/status").catch((e) => ({ data: { _error: e?.response?.data?.detail || e.message } })),
        api.get("/admin/retention/status").catch((e) => ({ data: { _error: e?.response?.data?.detail || e.message } })),
      ]);
      setArbiter(a.data);
      setTradingCtl(t.data);
      setTicks(k.data?.ticks || []);
      setRoster(r.data);
      setRouter(ro.data);
      setRetention(re.data);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setBusyRefresh(false);
    }
  }, []);

  useEffect(() => {
    load();
    // Auto-refresh every 15s so the ticks column matches the pulse cadence.
    const id = setInterval(load, 15_000);
    return () => clearInterval(id);
  }, [load]);

  const flipArbiter = async () => {
    const current = arbiter?.runtime_mode;
    const next = current === "LIVE" ? "DISARMED" : "LIVE";
    const confirmed = window.confirm(
      `Flip arbiter runtime mode: ${current} → ${next}?\n\n` +
      (next === "LIVE"
        ? "This ENABLES intent emission from every pulse arbitration. Combined with the master switch armed, this puts real orders on the wire."
        : "This stops intent emission. In-flight orders continue; new arbitrations produce decisions only, no intents.")
    );
    if (!confirmed) return;
    setBusyArbiter(true);
    try {
      const r = await api.post("/mc/arbiter/runtime-mode", { mode: next });
      setArbiter((prev) => ({ ...(prev || {}), runtime_mode: r.data?.runtime_mode || next }));
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setBusyArbiter(false);
      // Immediate reload so the display reflects the flip within one paint.
      load();
    }
  };

  const forceTick = async () => {
    setBusyForceTick(true);
    try {
      const r = await api.post("/admin/auto-router/force-tick");
      const picked = r.data?.results_count ?? 0;
      const exec = r.data?.executed_count ?? 0;
      const errMsg = r.data?.error;
      if (errMsg) {
        setErr(`Force tick error: ${typeof errMsg === "string" ? errMsg : JSON.stringify(errMsg)}`);
      } else {
        setErr("");
        alert(`Force tick complete: ${picked} picked · ${exec} exec`);
      }
    } catch (e) {
      const raw = e?.response?.data?.detail ?? e.message;
      setErr(typeof raw === "string" ? raw : JSON.stringify(raw));
    } finally {
      setBusyForceTick(false);
      load();
    }
  };

  const runPickProbe = async () => {
    setBusyProbe(true);
    try {
      const r = await api.get("/admin/auto-router/pick-probe");
      setProbe(r.data);
      setErr("");
    } catch (e) {
      const raw = e?.response?.data?.detail ?? e.message;
      setErr(typeof raw === "string" ? raw : JSON.stringify(raw));
    } finally {
      setBusyProbe(false);
    }
  };

  const runPurge = async () => {
    setBusyPurge(true);
    setPurgeResult(null);
    try {
      // One cycle is batch-capped server-side; big backlogs need
      // several clicks — the `capped` flags below say when to re-run.
      const r = await api.post("/admin/retention/run", null, { timeout: 290_000 });
      setPurgeResult(r.data);
      setErr("");
    } catch (e) {
      const raw = e?.response?.data?.detail ?? e.message;
      setErr(typeof raw === "string" ? raw : JSON.stringify(raw));
    } finally {
      setBusyPurge(false);
      load();
    }
  };

  const flipMaster = async () => {
    const current = !!tradingCtl?.trading_enabled_runtime;
    const next = !current;
    let reason = "";
    if (next) {
      reason = window.prompt(
        "Enable master switch — reason (required, audit trail):",
        "operator arm from dashboard"
      );
      if (!reason || !reason.trim()) return;
    } else {
      reason = window.prompt(
        "Disable master switch — reason (audit trail):",
        "operator disarm from dashboard"
      );
      if (reason === null) return;
    }
    setBusyMaster(true);
    try {
      const r = await api.post("/admin/trading/toggle", {
        enabled: next,
        reason: reason.trim(),
      });
      setTradingCtl((prev) => ({
        ...(prev || {}),
        trading_enabled_runtime: !!r.data?.enabled,
      }));
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setBusyMaster(false);
      load();
    }
  };

  const arbiterOn = arbiter?.runtime_mode === "LIVE";
  const masterOn = !!tradingCtl?.trading_enabled_runtime;
  const willFire = !!tradingCtl?.trading_will_fire;
  const loopClosed = arbiterOn && masterOn;

  // Per-brain presence signal for the seating strip.
  // Doctrine: honest data or nothing. We compute presence from the
  // MOST RECENT pulse tick that actually had brain completions
  // (any tick with brains_completed_count > 0). Ignoring blank
  // ticks avoids false-red when Camino skips its off-cadence tick.
  //   green   → brain was in brains_completed on that tick
  //   red     → brain was in brains_failed on that tick
  //   yellow  → tick ran but this brain neither completed nor failed
  //   grey    → no populated tick yet (dot suppressed)
  const brainPresence = React.useMemo(() => {
    const latestPopulated = ticks.find(
      (t) => (t.brains_completed_count || 0) + (t.brains_failed_count || 0) > 0
    );
    if (!latestPopulated) return { source: null, byBrain: {} };
    const completed = new Set((latestPopulated.brains_completed || []).map((b) => String(b).toLowerCase()));
    const failed = new Set((latestPopulated.brains_failed || []).map((b) => String(b).toLowerCase()));
    return {
      source: latestPopulated,
      byBrain: {
        camino:    completed.has("camino")    ? "ok" : failed.has("camino")    ? "fail" : "absent",
        barracuda: completed.has("barracuda") ? "ok" : failed.has("barracuda") ? "fail" : "absent",
        hellcat:   completed.has("hellcat")   ? "ok" : failed.has("hellcat")   ? "fail" : "absent",
        gto:       completed.has("gto")       ? "ok" : failed.has("gto")       ? "fail" : "absent",
      },
    };
  }, [ticks]);

  const presenceMeta = {
    ok:     { color: "#10B981", label: "present in latest populated tick" },
    fail:   { color: "#EF4444", label: "failed on latest populated tick" },
    absent: { color: "#F59E0B", label: "did NOT opt in on latest populated tick" },
  };

  // Aggregate a 15-tick summary for the header pill.
  const recent = ticks.slice(0, 15);
  const totalArbs = recent.reduce((s, t) => s + (t.arbitrations_completed || 0), 0);
  const totalIntents = recent.reduce((s, t) => s + (t.intents_emitted || 0), 0);
  const anyBrains = recent.some((t) => (t.brains_completed_count || 0) > 0);

  return (
    <Card className="mb-6" testid="operator-control-tile" accentColor={loopClosed ? "#10B981" : "#EF4444"}>
      <GateProgressBar />
      <EntryModePanel />
      <div className="flex items-start justify-between gap-3 mb-4">
        <div>
          <div className="flex items-center gap-2">
            <Lightning size={16} weight="fill" color={loopClosed ? "#10B981" : "#71717A"} />
            <h3 className="font-display text-lg font-bold tracking-tight">
              Operator Control
            </h3>
            <Badge color={loopClosed ? "#10B981" : "#EF4444"} testid="operator-control-status">
              {loopClosed ? "LOOP CLOSED" : "LOOP OPEN"}
            </Badge>
          </div>
          <p className="text-xs text-rd-muted mt-1 font-mono">
            Two switches control live trading. Both must be ON for the
            pulse → arbiter → intent → broker chain to fire.
          </p>
        </div>
        <button
          onClick={load}
          disabled={busyRefresh}
          className="p-2 border border-rd-border hover:border-rd-text disabled:opacity-50"
          data-testid="operator-control-refresh"
          title="Refresh"
        >
          <ArrowsClockwise size={14} className={busyRefresh ? "animate-spin" : ""} />
        </button>
      </div>

      {err && (
        <div className="mb-3 p-2 border border-red-500 text-red-400 text-xs font-mono" data-testid="operator-control-error">
          <Warning size={12} className="inline mr-1" /> {err}
        </div>
      )}

      {/* Toggles */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-5">
        <div className="border border-rd-border p-3" data-testid="toggle-arbiter-block">
          <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-1 font-mono">
            1. Arbiter Runtime Mode
          </div>
          <Toggle
            label="ARBITER"
            checked={arbiterOn}
            busy={busyArbiter}
            onToggle={flipArbiter}
            testid="toggle-arbiter"
          />
          <div className="text-[10px] text-rd-muted mt-2 font-mono leading-relaxed">
            LIVE → pulse arbitrations emit intents into shared_intents.
            DISARMED → decisions recorded only, no intents.
          </div>
        </div>

        <div className="border border-rd-border p-3" data-testid="toggle-master-block">
          <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-1 font-mono">
            2. Master Switch
          </div>
          <Toggle
            label="TRADING"
            checked={masterOn}
            busy={busyMaster}
            onToggle={flipMaster}
            testid="toggle-master"
          />
          <div className="text-[10px] text-rd-muted mt-2 font-mono leading-relaxed">
            ON → auto_router routes pending intents to brokers.
            OFF → intents stay pending (fail-closed).
          </div>
          {masterOn && !willFire && (
            <div className="text-[10px] text-yellow-500 mt-2 font-mono">
              <Warning size={10} className="inline mr-1" />
              env AUTO_ROUTER_ENABLED is false — env veto in effect
            </div>
          )}
        </div>
      </div>

      {/* Current seating — single-source-of-truth from Mongo roster.
          Operators can verify who holds each seat without navigating
          to Intents → Quick Seat Switches. Read-only here; assignment
          still happens on the dedicated Intents surface. */}
      {roster?.assignments && (
        <div className="border border-rd-border p-3 mb-5" data-testid="operator-control-seating">
          <div className="flex items-center justify-between mb-2">
            <div className="text-[10px] uppercase tracking-widest text-rd-dim font-mono">
              Current Seating
            </div>
            <a
              href="/admin/intents"
              className="text-[10px] text-rd-dim hover:text-rd-text font-mono underline decoration-dotted"
              data-testid="operator-control-seating-link"
            >
              assign / vacate ↗
            </a>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {[
              { lane: "EQUITY", color: "#F59E0B", roles: [
                ["executor",   "Executor",   roster.assignments.executor],
                ["strategist", "Strategist", roster.assignments.strategist],
                ["governor",   "Governor",   roster.assignments.governor],
                ["auditor",    "Auditor",    roster.assignments.auditor],
              ]},
              { lane: "CRYPTO", color: "#7B5CFF", roles: [
                ["crypto",            "Executor",   roster.assignments.crypto],
                ["crypto_strategist", "Strategist", roster.assignments.crypto_strategist],
                ["crypto_governor",   "Governor",   roster.assignments.crypto_governor],
                ["crypto_auditor",    "Auditor",    roster.assignments.crypto_auditor],
              ]},
            ].map((laneBlock) => (
              <div key={laneBlock.lane} data-testid={`seating-lane-${laneBlock.lane.toLowerCase()}`}>
                <div
                  className="text-[10px] uppercase tracking-widest font-mono mb-1.5"
                  style={{ color: laneBlock.color }}
                >
                  {laneBlock.lane}
                </div>
                <div className="grid grid-cols-4 gap-1.5">
                  {laneBlock.roles.map(([role, label, brain]) => {
                    const state = brain ? brainPresence.byBrain[String(brain).toLowerCase()] : null;
                    const meta = state ? presenceMeta[state] : null;
                    return (
                      <div
                        key={role}
                        className="border border-rd-border/60 px-1.5 py-1"
                        data-testid={`seating-${role}`}
                        title={
                          brain
                            ? `${label}: ${brain}${meta ? ` · ${meta.label}` : ""}`
                            : `${label}: vacant`
                        }
                      >
                        <div className="text-[9px] uppercase tracking-widest text-rd-dim font-mono">
                          {label}
                        </div>
                        <div className="flex items-center gap-1.5">
                          {meta && (
                            <span
                              className="inline-block w-1.5 h-1.5 rounded-full flex-shrink-0"
                              style={{ backgroundColor: meta.color }}
                              data-testid={`seating-${role}-dot`}
                              data-presence={state}
                            />
                          )}
                          <div
                            className="text-xs font-mono font-bold uppercase truncate"
                            style={{ color: brain ? "#E4E4E7" : "#71717A" }}
                          >
                            {brain || "vacant"}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Auto-router health — the loop that turns emitted intents
          into broker calls. If `tick_count` is growing but
          `last_tick_executed` stays 0, every intent is being picked
          up but silently failing/timing out. If `task_alive=false`,
          no BUY/SELL will ever reach a broker. */}
      {router && (
        <div className="border border-rd-border p-3 mb-5" data-testid="operator-control-router">
          <div className="flex items-center justify-between mb-2">
            <div className="text-[10px] uppercase tracking-widest text-rd-dim font-mono">
              Auto-Router (intent → broker)
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={runPickProbe}
                disabled={busyProbe}
                data-testid="router-pick-probe"
                className="text-[10px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-0.5 disabled:opacity-40 disabled:cursor-not-allowed"
                title="Run the router's exact pick query with a step-by-step filter breakdown — shows exactly why 0 intents get picked"
              >
                {busyProbe ? "probing…" : "pick probe"}
              </button>
              <button
                onClick={forceTick}
                disabled={busyForceTick}
                data-testid="router-force-tick"
                className="text-[10px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-0.5 disabled:opacity-40 disabled:cursor-not-allowed"
                title="Run one auto-router tick immediately instead of waiting for the 30s interval"
              >
                {busyForceTick ? "ticking…" : "force tick"}
              </button>
              <span
                className="text-[10px] font-mono font-bold uppercase tracking-widest"
                style={{ color: router.task_alive ? "#10B981" : "#EF4444" }}
                data-testid="router-task-state"
              >
                {router.task_alive ? "TASK ALIVE" : "TASK DEAD"}
              </span>
            </div>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs font-mono">
            <div>
              <div className="text-[9px] uppercase tracking-widest text-rd-dim">Enabled env</div>
              <div
                className="font-bold uppercase"
                style={{ color: router.enabled_env ? "#10B981" : "#EF4444" }}
                data-testid="router-enabled-env"
              >
                {router.enabled_env ? "true" : "false"}
              </div>
            </div>
            <div>
              <div className="text-[9px] uppercase tracking-widest text-rd-dim">Tick count</div>
              <div className="font-bold" data-testid="router-tick-count">
                {router.tick_count ?? 0}
              </div>
            </div>
            <div>
              <div className="text-[9px] uppercase tracking-widest text-rd-dim">Last tick age</div>
              <div className="font-bold" data-testid="router-last-tick-age">
                {relTime(router.last_tick_ts)}
              </div>
            </div>
            <div>
              <div className="text-[9px] uppercase tracking-widest text-rd-dim">Last tick</div>
              <div className="font-bold" data-testid="router-last-tick-executed">
                <span style={{ color: (router.last_tick_results || 0) > 0 ? "#E4E4E7" : "#71717A" }}>
                  {router.last_tick_results ?? 0} picked
                </span>
                {" · "}
                <span style={{ color: (router.last_tick_executed || 0) > 0 ? "#10B981" : "#EF4444" }}>
                  {router.last_tick_executed ?? 0} exec
                </span>
              </div>
            </div>
          </div>
          {router.last_tick_error && (
            <div className="mt-2 border border-rd-danger px-2 py-1 text-[10px] font-mono text-rd-danger" data-testid="router-last-tick-error">
              <Warning size={10} className="inline mr-1" />
              last error: {router.last_tick_error}
            </div>
          )}
          {router.last_intent_error && (
            <div className="mt-2 border border-rd-danger px-2 py-1 text-[10px] font-mono text-rd-danger" data-testid="router-last-intent-error">
              <Warning size={10} className="inline mr-1" />
              route_one crashing ({router.last_tick_exceptions || 0}× last tick): {router.last_intent_error}
            </div>
          )}
          {router.last_tick_disarmed && (
            <div className="mt-2 border border-yellow-600 px-2 py-1 text-[10px] font-mono text-yellow-500" data-testid="router-last-tick-disarmed">
              <ShieldSlash size={10} className="inline mr-1" />
              Last tick skipped intake: MASTER SWITCH read as DISARMED by the router. Intents stay pending while this shows.
            </div>
          )}
          {router.master_switch_read_degraded && (
            <div className="mt-2 border border-orange-600 px-2 py-1 text-[10px] font-mono text-orange-400" data-testid="router-switch-read-degraded">
              <Warning size={10} className="inline mr-1" />
              MASTER SWITCH read DEGRADED — Mongo read failing ({router.master_switch_read_error || "unknown"}). Router is using last-known state: {router.master_switch_last_known === true ? "ARMED" : router.master_switch_last_known === false ? "DISARMED" : "none (fail-closed)"}. Check Atlas load.
            </div>
          )}
          {probe && (
            <div className="mt-3 border border-rd-border p-2" data-testid="router-pick-probe-result">
              <div className="flex items-center justify-between mb-1.5">
                <div className="text-[9px] uppercase tracking-widest text-rd-dim font-mono">
                  Pick probe · lookback {probe.lookback_min}m
                </div>
                <div
                  className="text-[10px] font-mono font-bold"
                  style={{ color: (probe.routable_now || 0) > 0 ? "#10B981" : "#EF4444" }}
                  data-testid="router-pick-probe-routable"
                >
                  {probe.routable_now ?? "?"} routable now
                </div>
              </div>
              <div className="space-y-0.5">
                {(probe.filter_breakdown || []).map((s, i, arr) => {
                  const prev = i > 0 ? arr[i - 1].count : null;
                  const collapsed = typeof s.count === "number" && s.count === 0 && (prev === null || prev > 0);
                  return (
                    <div
                      key={s.filter_added}
                      className="flex items-center justify-between text-[10px] font-mono"
                      data-testid={`probe-step-${s.filter_added}`}
                    >
                      <span className={collapsed ? "text-rd-danger font-bold" : "text-rd-muted"}>
                        {collapsed ? "▶ " : ""}+{s.filter_added}
                      </span>
                      <span className={collapsed ? "text-rd-danger font-bold" : "text-rd-text"}>
                        {s.error ? `err: ${s.error}` : s.count}
                      </span>
                    </div>
                  );
                })}
              </div>
              {(probe.routable_now || 0) > 0 && (
                <div className="mt-1.5 text-[10px] font-mono text-yellow-500 leading-relaxed">
                  Intents ARE matchable — if "last tick" still says 0 picked, route_one is failing on them (see error strip above after next tick).
                </div>
              )}
            </div>
          )}
          {router.task_exception && (
            <div className="mt-2 border border-rd-danger px-2 py-1 text-[10px] font-mono text-rd-danger" data-testid="router-task-exception">
              <Warning size={10} className="inline mr-1" />
              task exception: {router.task_exception}
            </div>
          )}
          {router.task_alive && (router.last_tick_results || 0) > 0 && (router.last_tick_executed || 0) === 0 && !router.last_tick_error && (
            <div className="mt-2 text-[10px] font-mono text-yellow-500 leading-relaxed">
              <Warning size={10} className="inline mr-1" />
              Router picked {router.last_tick_results} intents last tick but executed 0 with no error. Every intent is being blocked or timing out silently — check seat / risk / broker stages in server logs.
            </div>
          )}
        </div>
      )}

      {/* Retention / backlog drain — 72h expiry sweeper. Prod Atlas
          chokes ("operation exceeded time limit") until the stale
          telemetry backlog is drained; this gives the operator a
          one-click drain instead of a raw API call. */}
      {retention && !retention._error && (
        <div className="border border-rd-border p-3 mb-5" data-testid="operator-control-retention">
          <div className="flex items-center justify-between mb-2">
            <div className="text-[10px] uppercase tracking-widest text-rd-dim font-mono">
              Retention · {retention.retention_days}d backlog expiry
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={runPurge}
                disabled={busyPurge || retention.running_now}
                data-testid="retention-purge-now"
                className="text-[10px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-0.5 disabled:opacity-40 disabled:cursor-not-allowed"
                title="Run one purge cycle now (batched — big backlogs need several clicks; watch the 'more remains' flag)"
              >
                {busyPurge || retention.running_now ? "purging…" : "purge backlog now"}
              </button>
              <span
                className="text-[10px] font-mono font-bold uppercase tracking-widest"
                style={{ color: retention.task_alive ? "#10B981" : "#EF4444" }}
                data-testid="retention-task-state"
              >
                {retention.task_alive ? "SWEEPER ALIVE" : "SWEEPER DEAD"}
              </span>
            </div>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs font-mono">
            <div>
              <div className="text-[9px] uppercase tracking-widest text-rd-dim">Cycles run</div>
              <div className="font-bold" data-testid="retention-cycle-count">{retention.cycle_count ?? 0}</div>
            </div>
            <div>
              <div className="text-[9px] uppercase tracking-widest text-rd-dim">Total purged</div>
              <div className="font-bold" data-testid="retention-total-deleted">
                {(retention.total_deleted ?? 0).toLocaleString()}
              </div>
            </div>
            <div>
              <div className="text-[9px] uppercase tracking-widest text-rd-dim">Last run</div>
              <div className="font-bold" data-testid="retention-last-run">
                {retention.last_run_at ? `${retention.last_run_sec}s` : "—"}
              </div>
            </div>
            <div>
              <div className="text-[9px] uppercase tracking-widest text-rd-dim">Kept forever</div>
              <div className="font-bold text-[10px] leading-tight">executed intents · fills</div>
            </div>
          </div>
          {retention.last_error && (
            <div className="mt-2 border border-rd-danger px-2 py-1 text-[10px] font-mono text-rd-danger" data-testid="retention-last-error">
              <Warning size={10} className="inline mr-1" />
              {retention.last_error}
            </div>
          )}
          {purgeResult && (
            <div className="mt-2 border border-rd-border p-2 text-[10px] font-mono" data-testid="retention-purge-result">
              <span className="font-bold" style={{ color: purgeResult.ok ? "#10B981" : "#EF4444" }}>
                {purgeResult.ok ? "PURGED" : "FAILED"} {(purgeResult.deleted ?? 0).toLocaleString()} docs in {purgeResult.took_sec}s
              </span>
              {Object.values(purgeResult.collections || {}).some((c) => c.capped) && (
                <span className="text-yellow-500 ml-2" data-testid="retention-more-remains">
                  · more remains — click purge again to keep draining
                </span>
              )}
              {(purgeResult.deleted ?? 0) === 0 && purgeResult.ok && (
                <span className="text-rd-dim ml-2">· backlog fully drained</span>
              )}
            </div>
          )}
        </div>
      )}

      {/* Conviction floor knob + gate-failure digest (2026-07-21) */}
      <DailyBudgetTile />
      <OpportunityPolicyPanel />
      <ConvictionFloorKnob />
      <GateFailureDigest />
      <KrakenPairEditor />
      <WatchlistPanel />
      <TapeQualityPanel />
      <ExitMonitorPanel />
      <SellPointPanel />
      <ForensicsPanel />
      <OutcomeEnginePanel />
      <ScannerPanel />
      <GainGoalPanel />
      <OptionsPanel />
      <ExpectancyPanel />
      <OutcomePipelinePanel />

      {/* Last N pulse ticks */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <div className="text-[10px] uppercase tracking-widest text-rd-dim font-mono">
            Last {recent.length} Pulse Ticks · Σ arbitrations {totalArbs} · Σ intents {totalIntents}
          </div>
          {loopClosed && anyBrains && totalIntents === 0 && (
            <div className="text-[10px] text-yellow-500 font-mono">
              <ShieldSlash size={10} className="inline mr-1" />
              Loop closed but zero intents — brains all_flat OR consensus miss
            </div>
          )}
          {!anyBrains && recent.length > 0 && (
            <div className="text-[10px] text-yellow-500 font-mono">
              <Warning size={10} className="inline mr-1" />
              No brain completions — freshness gate rejecting snapshots
            </div>
          )}
        </div>

        {recent.length === 0 ? (
          <div className="text-xs text-rd-dim font-mono italic p-3 border border-dashed border-rd-border">
            No recent pulse ticks. The pulse worker may not be running.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs font-mono" data-testid="operator-control-ticks-table">
              <thead>
                <tr className="text-[10px] uppercase tracking-widest text-rd-dim border-b border-rd-border">
                  <th className="text-left py-1.5 pr-3">Age</th>
                  <th className="text-right pr-3">Snaps</th>
                  <th className="text-right pr-3">Brains</th>
                  <th className="text-right pr-3">Arbs</th>
                  <th className="text-right pr-3">Intents</th>
                  <th className="text-left pr-3">Mode</th>
                  <th className="text-center pr-1">OK</th>
                </tr>
              </thead>
              <tbody>
                {recent.map((t) => {
                  const ok = t.orchestration_ok !== false;
                  const brains = `${t.brains_completed_count || 0}/${(t.brains_completed_count || 0) + (t.brains_failed_count || 0)}`;
                  const modeColor = t.runtime_mode === "LIVE" ? "#10B981" : "#71717A";
                  const intentsColor = (t.intents_emitted || 0) > 0 ? "#10B981" : "#71717A";
                  return (
                    <tr key={t.pulse_id} className="border-b border-rd-border/40 hover:bg-rd-bg1/30">
                      <td className="py-1.5 pr-3 text-rd-text">{relTime(t.started_at)}</td>
                      <td className="text-right pr-3 text-rd-text">{t.snapshot_count ?? "—"}</td>
                      <td
                        className="text-right pr-3"
                        title={(t.brains_completed || []).join(", ") || "none"}
                        style={{ color: (t.brains_completed_count || 0) === 4 ? "#10B981" : (t.brains_completed_count || 0) === 0 ? "#71717A" : "#F59E0B" }}
                      >
                        {brains}
                      </td>
                      <td className="text-right pr-3 text-rd-text">{t.arbitrations_completed ?? 0}</td>
                      <td className="text-right pr-3 font-bold" style={{ color: intentsColor }}>
                        {t.intents_emitted ?? 0}
                      </td>
                      <td className="pr-3" style={{ color: modeColor }}>
                        {t.runtime_mode || "—"}
                      </td>
                      <td className="text-center pr-1">
                        {ok
                          ? <CheckCircle size={12} color="#10B981" weight="fill" />
                          : <Warning size={12} color="#EF4444" weight="fill" />}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <div className="text-[10px] text-rd-muted mt-3 font-mono leading-relaxed">
          Non-zero <span style={{ color: "#10B981" }}>Intents</span> = pulse→arbiter→intent
          loop is firing. If Intents stays 0 while Brains shows 4/4 and Mode is LIVE,
          every seat is resolving all_flat — check confidence thresholds / regime gates.
        </div>

      </div>
    </Card>
  );
}
