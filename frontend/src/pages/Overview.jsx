import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, RUNTIME_META, fmtTime, relTime } from "@/lib/api";
import { PageHeader, Card, Badge, LoadingRow } from "@/components/ui-bits";
import { ArrowUpRight } from "@phosphor-icons/react";
import TechnicalsPanel from "@/components/TechnicalsPanel";
import FeedersStrip from "@/components/FeedersStrip";
import RosterPanel from "@/components/ParadoxRosterPanel";
import AssignableRosterPanel from "@/components/RosterPanel";
import LivePositionsPanel from "@/components/LivePositionsPanel";
import PanelErrorBoundary from "@/components/PanelErrorBoundary";
import MarketRegimeTape from "@/components/MarketRegimeTape";
import PositionMisreadsCard from "@/components/PositionMisreadsCard";
import DivergenceChopGauge from "@/components/DivergenceChopGauge";
import TradeTape from "@/components/TradeTape";
import TraderSeatViewer from "@/components/TraderSeatViewer";

export default function Overview() {
  const [overview, setOverview] = useState(null);
  const [flags, setFlags] = useState(null);
  const [diag, setDiag] = useState(null);
  const [staleConflicts, setStaleConflicts] = useState(null);
  const [patternScan, setPatternScan] = useState(null);
  const [sidecarDiag, setSidecarDiag] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const [o, f, d, sc, ps, sd] = await Promise.all([
          api.get("/shared/overview"),
          api.get("/admin/flags"),
          api.get("/admin/diagnostics"),
          // Fail-soft on each diagnostic — none of them must blank the page.
          api.get("/admin/conflicts/stale?older_than_hours=24")
            .catch((e) => ({ data: { _error: e?.response?.data?.detail || e.message } })),
          api.get("/admin/patterns/scan?limit=10&min_score=0.5")
            .catch((e) => ({ data: { _error: e?.response?.data?.detail || e.message } })),
          api.get("/admin/sidecar-diagnostics")
            .catch((e) => ({ data: { _error: e?.response?.data?.detail || e.message } })),
        ]);
        setOverview(o.data);
        setFlags(f.data);
        setDiag(d.data);
        setStaleConflicts(sc.data);
        setPatternScan(ps.data);
        setSidecarDiag(sd.data);
      } catch (e) {
        setErr(e?.response?.data?.detail || e.message);
      }
    })();
  }, []);

  const ready = overview && flags && diag;

  return (
    <div className="reveal" data-testid="overview-page">
      <PageHeader
        eyebrow="Mission Control · Overview"
        title="Four brains. One nervous system."
        sub="Shared infrastructure connects Alpha, Camaro, Chevelle, and REDEYE. Authority lives on SEATS, not on brain identity — any brain can hold any seat; the seat carries the doctrine and grants the rights. The seat is what gets graded, promoted, retired. Every brain stamps stances on the shared position primitive; only the seat holder of the moment makes the call."
        right={
          <div className="hidden md:flex items-center gap-2" data-testid="overview-mode-pill">
            <Badge color={flags?.deploy_mode === "execution" ? "#10B981" : "#A1A1AA"}>
              {flags?.deploy_mode || "—"}
            </Badge>
          </div>
        }
        testid="overview-header"
      />

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-4 text-xs font-mono" data-testid="overview-error">
          {err}
        </div>
      )}

      {!ready && <LoadingRow testid="overview-loading" />}

      {ready && (
        <>
          {/* Runtime cards */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 md:gap-6 mb-6" data-testid="runtime-cards">
            {overview.runtimes.map((rt) => {
              const meta = RUNTIME_META[rt.runtime] || {
                label: rt.runtime.toUpperCase(), project: "—", color: "#A1A1AA",
                roleTitle: rt.role || "—", note: "",
                enforceFlag: null,
              };
              const enforce = meta.enforceFlag
                ? flags.enforce_flags[meta.enforceFlag]
                : null;
              return (
                <Card
                  key={rt.runtime}
                  accentColor={meta.color}
                  testid={`runtime-card-${rt.runtime}`}
                  className="hover:bg-[#141414] transition-colors group"
                >
                  <div className="flex items-start justify-between mb-4">
                    <div>
                      <div className="label-eyebrow mb-1">{meta.project}</div>
                      <div
                        className="font-display text-2xl font-black tracking-tighter"
                        style={{ color: meta.color }}
                      >
                        {meta.label}
                      </div>
                      <div className="mt-2 flex items-center gap-2 flex-wrap">
                        <Badge color={meta.color} testid={`runtime-role-${rt.runtime}`}>
                          {meta.roleTitle.toUpperCase()}
                        </Badge>
                        <Badge
                          color={rt.authority_state === "governor" ? "#A1A1AA" : meta.color}
                          testid={`runtime-authority-${rt.runtime}`}
                        >
                          {(rt.authority_state || "").replace("_", " ").toUpperCase()}
                        </Badge>
                      </div>
                      <div className="text-[10px] text-rd-muted uppercase tracking-widest mt-2 leading-relaxed">
                        {meta.note}
                      </div>
                    </div>
                    <Link
                      to={`/admin/brain/${rt.runtime}`}
                      className="opacity-60 group-hover:opacity-100 text-rd-muted hover:text-rd-text"
                      data-testid={`runtime-card-link-${rt.runtime}`}
                    >
                      <ArrowUpRight size={18} weight="bold" />
                    </Link>
                  </div>

                  <div className="space-y-1.5">
                    <Row label="MODE" value={
                      <Badge color="#FBBF24">{rt.mode}</Badge>
                    } />
                    <Row label="EXECUTION" value={
                      <Badge color={rt.execution_allowed ? "#10B981" : "#71717A"}>
                        {rt.execution_allowed ? "AUTHORIZED" : "OBSERVATION"}
                      </Badge>
                    } />
                    {meta.enforceFlag && (
                      <Row label="ENFORCE" value={
                        <Badge color={enforce ? "#10B981" : "#71717A"}>
                          {enforce ? "ENABLED" : "DISABLED"}
                        </Badge>
                      } />
                    )}
                    <Row label="ARTIFACT" value={
                      <span className="font-mono text-xs">
                        {rt.latest_artifact?.version || "—"}
                      </span>
                    } />
                    <Row label="RECEIPTS" value={
                      <span className="font-mono text-sm" style={{ color: meta.color }}>
                        {rt.receipts_count}
                      </span>
                    } />
                    <Row label="MEMORY LABELS" value={
                      <span className="font-mono text-sm">{rt.memory_labels_count}</span>
                    } />
                    {rt.heartbeat_stale && (
                      <>
                        <Row label="HEARTBEAT" value={
                          <Badge color="#EF4444" testid={`heartbeat-stale-${rt.runtime}`}>
                            STALE — {rt.heartbeat_age_seconds == null
                              ? "no signal"
                              : `${Math.floor(rt.heartbeat_age_seconds)}s`}
                          </Badge>
                        } />
                        <Row label="" value={
                          <a
                            href={`/ping/${rt.runtime}`}
                            target="_blank"
                            rel="noreferrer"
                            className="text-[10px] text-rd-muted hover:text-rd-text font-mono underline"
                            data-testid={`ping-link-${rt.runtime}`}
                          >
                            open ping page →
                          </a>
                        } />
                      </>
                    )}
                    <Row label="LAST SIGNAL" value={
                      <span className="font-mono text-xs text-rd-muted">
                        {rt.last_receipt ? relTime(rt.last_receipt.timestamp) : "—"}
                      </span>
                    } />
                  </div>
                </Card>
              );
            })}
          </div>

          {/* P3 (2026-07-01) — Sidecar Trader observability.
              Two tiles read from `/api/admin/trader/*`, which now
              serves from local SQLite so these keep working during
              an Atlas outage. TradeTape is the primary "what did
              the trader do this minute?" surface; TraderSeatViewer
              shows the 4×2 seat matrix + Mongo→cache freshness. */}
          <div
            className="grid grid-cols-1 lg:grid-cols-3 gap-4 md:gap-6 mb-6"
            data-testid="overview-trader-strip"
          >
            <PanelErrorBoundary panelName="Trade Tape" testid="panel-error-trade-tape">
              <div className="lg:col-span-2">
                <TradeTape />
              </div>
            </PanelErrorBoundary>
            <PanelErrorBoundary panelName="Trader Seats" testid="panel-error-trader-seats">
              <TraderSeatViewer />
            </PanelErrorBoundary>
          </div>

          {/* P2 (2026-06-10) — Live observability strip.
              Three cards consume the new `/api/mc-connection/stream`
              SSE feed:
                * Market Regime Tape — current regime + transition history
                * Position Misreads — last 20, with 24h verdict
                * Divergence/Chop Gauge — composite of regime + hold ratio
              All three share ONE SSE connection via `useMcStream`. */}
          <div
            className="grid grid-cols-1 lg:grid-cols-3 gap-4 md:gap-6 mb-6"
            data-testid="overview-live-strip"
          >
            <PanelErrorBoundary>
              <MarketRegimeTape />
            </PanelErrorBoundary>
            <PanelErrorBoundary>
              <DivergenceChopGauge />
            </PanelErrorBoundary>
            <PanelErrorBoundary>
              <PositionMisreadsCard />
            </PanelErrorBoundary>
          </div>

          {/* Stale conflicts alert — open conflicts older than 24h.
              Surfaces hypotheses that nobody resolved so the operator
              can sweep them. Doctrine: alert only, never authority. */}
          {staleConflicts && !staleConflicts._error && (
            <StaleConflictsTile data={staleConflicts} />
          )}

          {/* Sidecar Diagnostics — fleet health at a glance.
              Doctrine: read-only. Surfaces the "21k mystery" answer
              (audit log totals are healthy heartbeats, not backlogs)
              and per-brain operator hints. */}
          {sidecarDiag && !sidecarDiag._error && (
            <SidecarDiagnosticsTile data={sidecarDiag} />
          )}

          {/* Pattern Watch — top symbols showing the textbook
              base-formation → consolidation → breakout pattern.
              Doctrine: DESCRIPTIVE EVIDENCE ONLY. Never authority. */}
          {patternScan && !patternScan._error && (
            <PatternWatchTile data={patternScan} />
          )}

          {/* Shared Technical Feed — Mission-page panel.
              Each panel is isolated by an ErrorBoundary so one bad
              render (PROD blank-screen regression, 2026-02-17) can
              only damage its own slot, not blank the whole page. */}
          <PanelErrorBoundary panelName="Brain Roster" testid="panel-error-roster">
            <RosterPanel />
          </PanelErrorBoundary>
          <PanelErrorBoundary panelName="Roster Assignment" testid="panel-error-roster-assign">
            <AssignableRosterPanel />
          </PanelErrorBoundary>
          <PanelErrorBoundary panelName="Live Positions" testid="panel-error-live-positions">
            <LivePositionsPanel />
          </PanelErrorBoundary>
          <PanelErrorBoundary panelName="Feeders" testid="panel-error-feeders">
            <FeedersStrip />
          </PanelErrorBoundary>
          <div className="mb-6">
            <PanelErrorBoundary panelName="Shared Technical Feed" testid="panel-error-technicals">
              <TechnicalsPanel />
            </PanelErrorBoundary>
          </div>

          {/* Doctrine + Flags strip */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 md:gap-6">
          <Card className="lg:col-span-2" testid="doctrine-card">
            <div className="label-eyebrow mb-3">Seat doctrine</div>
            <div className="font-display text-xl font-bold tracking-tight leading-snug mb-4">
              Seats carry authority.<br />
              Brains carry training.<br />
              <span className="text-rd-warn">The seat is what gets graded.</span>
            </div>
            <ul className="grid grid-cols-1 md:grid-cols-3 gap-x-6 gap-y-1 text-xs font-mono text-rd-muted">
              <li><span className="text-rd-text">EXECUTOR</span> — routes broker orders. Required for quorum. Any eligible brain.</li>
              <li><span className="text-rd-text">STRATEGIST</span> — forms the trust / reduce / veto / observation call on each intent (conviction signal).</li>
              <li><span className="text-rd-text">GOVERNOR</span> — memory firewall, readiness, calibration, promotion control.</li>
              <li><span className="text-rd-text">AUDITOR</span> — post-trade review + contrary case. Scores doctrine, never decides.</li>
            </ul>
            <div className="text-[10px] text-rd-dim uppercase tracking-widest mt-4">
              Performance attaches to (lane, seat, doctrine_version) — never to a brain. Promotions and retirements target the seat doctrine. Holders rotate.
            </div>
          </Card>

            <Card testid="flags-strip">
              <div className="label-eyebrow mb-3">Runtime flags</div>
              <div className="space-y-2">
                <FlagLine name="BROKER_LIVE_ORDER_ENABLED" on={flags.broker_live_order_enabled} />
              </div>
              <div className="text-[10px] text-rd-dim uppercase tracking-widest mt-4">
                Mongo · {diag.mongo.ok ? "online" : "offline"} · last sync {fmtTime(diag.now)}
              </div>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}

function Row({ label, value }) {
  return (
    <div className="flex items-center justify-between py-1 border-b border-rd-border last:border-b-0">
      <span className="text-[10px] uppercase tracking-widest text-rd-dim">{label}</span>
      <span>{value}</span>
    </div>
  );
}

function FlagLine({ name, on }) {
  return (
    <div className="flex items-center justify-between" data-testid={`flag-line-${name}`}>
      <span className="font-mono text-[11px] text-rd-muted truncate pr-2">{name}</span>
      <Badge color={on ? "#10B981" : "#71717A"}>{on ? "TRUE" : "FALSE"}</Badge>
    </div>
  );
}

function StaleConflictsTile({ data }) {
  const count = data?.count || 0;
  const oldest = data?.oldest_age_hours;
  const byRuntime = data?.by_runtime || {};

  // Heat band — drives the color. >0 = warn, >5 or 72h+ = danger.
  const danger = count >= 5 || (oldest != null && oldest >= 72);
  const warn = count > 0 && !danger;
  const color = danger ? "#EF4444" : warn ? "#FBBF24" : "#10B981";
  const label = danger ? "ACTION REQUIRED" : warn ? "ATTENTION" : "CLEAR";

  return (
    <Card
      accentColor={color}
      className="mb-6"
      testid="stale-conflicts-tile"
    >
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <div className="label-eyebrow mb-1">Conflict backlog</div>
          <div className="flex items-baseline gap-3">
            <span
              className="font-display text-4xl font-black tracking-tighter"
              style={{ color }}
              data-testid="stale-conflicts-count"
            >
              {count}
            </span>
            <span className="text-xs font-mono text-rd-muted">
              open · &gt; 24h
            </span>
            <Badge color={color} testid="stale-conflicts-label">
              {label}
            </Badge>
          </div>
          {oldest != null && count > 0 && (
            <div
              className="text-[11px] font-mono text-rd-muted mt-2"
              data-testid="stale-conflicts-oldest"
            >
              oldest: {oldest >= 24 ? `${(oldest / 24).toFixed(1)}d` : `${oldest.toFixed(1)}h`} unresolved
            </div>
          )}
          {count === 0 && (
            <div className="text-[11px] font-mono text-rd-muted mt-2">
              No hypotheses sitting longer than 24h. Discussion chain is clean.
            </div>
          )}
        </div>

        <div className="flex flex-col items-end gap-2 min-w-[180px]">
          <Link
            to="/admin/conflicts?status=open"
            className="text-[11px] uppercase tracking-widest font-mono text-rd-muted hover:text-rd-text underline"
            data-testid="stale-conflicts-link"
          >
            Triage queue →
          </Link>
          {Object.keys(byRuntime).length > 0 && (
            <div className="flex flex-wrap gap-1 justify-end" data-testid="stale-conflicts-by-runtime">
              {Object.entries(byRuntime)
                .sort((a, b) => b[1] - a[1])
                .map(([rt, n]) => {
                  const meta = RUNTIME_META[rt] || { color: "#A1A1AA", label: rt.toUpperCase() };
                  return (
                    <Badge
                      key={rt}
                      color={meta.color}
                      testid={`stale-conflicts-runtime-${rt}`}
                    >
                      {meta.label} · {n}
                    </Badge>
                  );
                })}
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}

function SidecarDiagnosticsTile({ data }) {
  const fleet = data?.fleet || {};
  const brains = data?.brains || [];
  // Color the tile by the worst verdict in the fleet — danger if any
  // brain is `dead`, warn if any `stale`/`partial`/`never`, else ok.
  const danger = fleet.dead > 0;
  const warn = !danger && (fleet.stale > 0 || fleet.partial > 0 || fleet.never > 0);
  const color = danger ? "#EF4444" : warn ? "#FBBF24" : "#10B981";
  const label = danger ? "ACTION REQUIRED" : warn ? "ATTENTION" : "FLEET HEALTHY";

  const verdictColor = {
    connected: "#10B981",
    partial: "#FBBF24",
    stale: "#FB923C",
    dead: "#EF4444",
    never: "#71717A",
  };

  return (
    <Card
      accentColor={color}
      className="mb-6"
      testid="sidecar-diagnostics-tile"
    >
      <div className="flex items-start justify-between gap-4 flex-wrap mb-4">
        <div>
          <div className="label-eyebrow mb-1">Sidecar fleet</div>
          <div className="flex items-baseline gap-3">
            <span
              className="font-display text-4xl font-black tracking-tighter"
              style={{ color }}
              data-testid="sidecar-diagnostics-connected-count"
            >
              {fleet.connected}/{fleet.total_brains}
            </span>
            <span className="text-xs font-mono text-rd-muted">connected</span>
            <Badge color={color} testid="sidecar-diagnostics-label">
              {label}
            </Badge>
          </div>
          <div className="text-[11px] font-mono text-rd-muted mt-2 flex flex-wrap gap-3">
            {fleet.partial > 0 && (
              <span data-testid="sidecar-diagnostics-partial">
                {fleet.partial} partial
              </span>
            )}
            {fleet.stale > 0 && (
              <span data-testid="sidecar-diagnostics-stale">
                {fleet.stale} stale
              </span>
            )}
            {fleet.dead > 0 && (
              <span data-testid="sidecar-diagnostics-dead" className="text-rd-danger">
                {fleet.dead} dead
              </span>
            )}
            {fleet.brains_with_no_intents_ever > 0 && (
              <span data-testid="sidecar-diagnostics-noemitters">
                {fleet.brains_with_no_intents_ever} never-emitted
              </span>
            )}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {brains.map((b) => {
          const meta = RUNTIME_META[b.brain] || { color: "#A1A1AA", label: b.brain.toUpperCase() };
          const v = b.verdict;
          return (
            <div
              key={b.brain}
              className="border border-rd-line/40 p-3 text-[11px] font-mono space-y-1"
              data-testid={`sidecar-row-${b.brain}`}
            >
              <div className="flex items-center justify-between mb-1">
                <span className="font-display text-sm tracking-tight" style={{ color: meta.color }}>
                  {meta.label}
                </span>
                <Badge color={verdictColor[v] || "#71717A"} testid={`sidecar-row-verdict-${b.brain}`}>
                  {v.toUpperCase()}
                </Badge>
              </div>
              <div className="text-rd-muted">{b.operator_hint}</div>
              <div className="grid grid-cols-2 gap-x-2 gap-y-0 mt-2">
                <span>intents</span>
                <span className="text-right" data-testid={`sidecar-row-intents-${b.brain}`}>
                  {b.intents.total}
                </span>
                <span>opinions</span>
                <span className="text-right" data-testid={`sidecar-row-opinions-${b.brain}`}>
                  {b.opinions.total}
                </span>
                <span>audit log</span>
                <span className="text-right" data-testid={`sidecar-row-audit-${b.brain}`}>
                  {b.sovereign_contribution.audit_log_total}
                </span>
                <span>heartbeat age</span>
                <span className="text-right">
                  {b.heartbeat.age_seconds != null
                    ? `${Math.round(b.heartbeat.age_seconds)}s`
                    : "—"}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}


function PatternWatchTile({ data }) {
  const items = data?.items || [];
  const tiers = data?.tier_counts || {};
  const hasBreakouts = tiers.breakout_active > 0;
  const color = hasBreakouts ? "#10B981" : items.length > 0 ? "#FBBF24" : "#71717A";

  return (
    <Card
      accentColor={color}
      className="mb-6"
      testid="pattern-watch-tile"
    >
      <div className="flex items-start justify-between gap-4 flex-wrap mb-3">
        <div>
          <div className="label-eyebrow mb-1">Pattern watch</div>
          <div className="flex items-baseline gap-3">
            <span
              className="font-display text-4xl font-black tracking-tighter"
              style={{ color }}
              data-testid="pattern-watch-count"
            >
              {items.length}
            </span>
            <span className="text-xs font-mono text-rd-muted">
              setups · score ≥ 0.5
            </span>
          </div>
          <div className="text-[11px] font-mono text-rd-muted mt-2 flex flex-wrap gap-3">
            {tiers.breakout_active > 0 && (
              <span data-testid="pattern-watch-breakouts" style={{ color: "#10B981" }}>
                {tiers.breakout_active} breakouts active
              </span>
            )}
            {tiers.consolidation_only > 0 && (
              <span data-testid="pattern-watch-consolidating">
                {tiers.consolidation_only} consolidating
              </span>
            )}
            {tiers.uptrend_only > 0 && (
              <span data-testid="pattern-watch-uptrend">
                {tiers.uptrend_only} uptrend only
              </span>
            )}
          </div>
        </div>
        <div className="text-[10px] font-mono text-rd-muted uppercase tracking-widest max-w-[260px] text-right">
          Descriptive evidence · brains decide
        </div>
      </div>

      {items.length === 0 ? (
        <div className="text-[11px] font-mono text-rd-muted">
          No qualifying setups right now. As brains and operators pull the technical feed, pattern snapshots populate and rank here.
        </div>
      ) : (
        <div className="space-y-1" data-testid="pattern-watch-list">
          {items.slice(0, 8).map((it) => (
            <div
              key={`${it.source}:${it.symbol}:${it.tf}`}
              className="flex items-center justify-between border border-rd-line/30 px-2 py-1 text-[11px] font-mono"
              data-testid={`pattern-watch-row-${it.symbol}`}
            >
              <div className="flex items-center gap-2">
                <span className="font-display text-sm tracking-tight">{it.symbol}</span>
                <span className="text-rd-muted">{it.tf}</span>
                {it.small_cap_qualified === true && (
                  <Badge color="#A78BFA">SMALL CAP</Badge>
                )}
              </div>
              <div className="flex items-center gap-3">
                {it.breakout && (
                  <Badge color="#10B981" testid={`pattern-watch-breakout-${it.symbol}`}>
                    BREAKOUT
                    {it.volume_surge_multiple
                      ? ` · ${it.volume_surge_multiple.toFixed(1)}× vol`
                      : ""}
                  </Badge>
                )}
                {!it.breakout && it.consolidation && (
                  <Badge color="#FBBF24">CONSOLIDATING · {it.consolidation_duration_bars}b</Badge>
                )}
                <span className="text-rd-text font-bold">
                  {(it.setup_score * 100).toFixed(0)}%
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

