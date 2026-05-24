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

export default function Overview() {
  const [overview, setOverview] = useState(null);
  const [flags, setFlags] = useState(null);
  const [diag, setDiag] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const [o, f, d] = await Promise.all([
          api.get("/shared/overview"),
          api.get("/admin/flags"),
          api.get("/admin/diagnostics"),
        ]);
        setOverview(o.data);
        setFlags(f.data);
        setDiag(d.data);
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
              <li><span className="text-rd-text">DECIDER</span> — forms the trust / reduce / veto / observation call on each intent.</li>
              <li><span className="text-rd-text">GOVERNOR</span> — memory firewall, readiness, calibration, promotion control.</li>
              <li><span className="text-rd-text">ADVISOR</span> — neutral counsel. Off-ladder.</li>
              <li><span className="text-rd-text">OPPONENT</span> — argues the contrary case. Off-ladder.</li>
              <li><span className="text-rd-text">AUDITOR</span> — post-trade review. Scores doctrine, never decides.</li>
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
