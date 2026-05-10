import React from "react";
import { Link } from "react-router-dom";
import { PageHeader, Card, Badge } from "@/components/ui-bits";
import { Eye, Lock, Warning, ArrowRight, ShieldCheck, Code } from "@phosphor-icons/react";

// REDEYE colour — distinct from the three trading brains. Deep red, advisor tone.
const REDEYE_COLOR = "#DC2626";
const CAMARO_COLOR = "#F59E0B";

const ALPHA_ALIGNMENT_TABLE = [
  { value: "null",          label: "null (default)",  meaning: "REDEYE has no view",                                       camaro: "Standard short-advisory flow",                                badge: "#A1A1AA" },
  { value: "aligned",       label: "aligned",         meaning: "Alpha is flat or bearish; REDEYE short reinforces",        camaro: "May size up modestly",                                       badge: "#10B981" },
  { value: "divergent",     label: "divergent",       meaning: "Alpha has no position; REDEYE short is independent",       camaro: "Independent short; standard flow",                           badge: "#3B82F6" },
  { value: "contradicts",   label: "contradicts",     meaning: "Alpha is currently long; REDEYE short directly contradicts", camaro: "Escalate. Patent J / dual-sign territory if elevation implied.", badge: REDEYE_COLOR },
];

const CONTRACT_FIELDS = [
  { k: "source",            v: "REDEYE",            note: "exact literal" },
  { k: "role",              v: "short_side_advisor", note: "never peer brain" },
  { k: "may_execute",       v: "false",             note: "REDEYE never places orders" },
  { k: "may_override_alpha", v: "false",            note: "REDEYE never overrules Alpha directly" },
  { k: "final_authority",   v: "CAMARO",            note: "buck stops at Camaro for REDEYE's advice — not a license to execute" },
];

const BRIDGE_CONFIG = [
  { k: "MIN_SHORT_SCORE",            v: "0.70",  note: "bear-score floor for SHORT action" },
  { k: "MIN_BEAR_CONFIDENCE",        v: "0.62",  note: "confidence floor for SHORT action" },
  { k: "MIN_REDEYE_RISK_MULTIPLIER", v: "0.25",  note: "lower bound, capped" },
  { k: "MAX_REDEYE_RISK_MULTIPLIER", v: "0.75",  note: "upper bound — REDEYE never claims full risk" },
];

export default function Redeye() {
  return (
    <div className="reveal" data-testid="redeye-page">
      <PageHeader
        eyebrow="Advisor · short-side scout"
        title="REDEYE"
        sub="Bearish/short-side adversarial scout. REDEYE sends advice to Camaro. Neither REDEYE nor Camaro can execute. Execution authority lives elsewhere on the ladder, gated by Patent J + operator countersign."
        right={
          <div className="flex items-center gap-2">
            <Badge color={REDEYE_COLOR}>
              <Eye size={11} weight="bold" className="inline mr-1 -mt-0.5" />
              ADVISOR
            </Badge>
            <Badge color="#FBBF24">
              <Lock size={11} weight="bold" className="inline mr-1 -mt-0.5" />
              ADMIN ONLY
            </Badge>
          </div>
        }
        testid="redeye-header"
      />

      {/* Doctrine flow strip — REDEYE advises Camaro; neither executes. */}
      <Card className="mb-6" testid="redeye-flow">
        <div className="label-eyebrow mb-3">Chain of authority</div>

        {/* REDEYE → Camaro (advisory pair, no execution) */}
        <div className="flex items-center justify-between flex-wrap gap-3 font-mono text-sm">
          <div className="flex items-center gap-2 font-bold" style={{ color: REDEYE_COLOR }}>
            <span className="inline-block w-2 h-2" style={{ background: REDEYE_COLOR }} />
            REDEYE
            <span className="text-rd-dim font-normal text-[10px] uppercase tracking-widest ml-1">short-side scout</span>
          </div>
          <ArrowRight size={14} className="text-rd-dim" />
          <div className="flex items-center gap-2 font-bold" style={{ color: CAMARO_COLOR }}>
            <span className="inline-block w-2 h-2" style={{ background: CAMARO_COLOR }} />
            CAMARO
            <span className="text-rd-dim font-normal text-[10px] uppercase tracking-widest ml-1">challenger · evaluates advice</span>
          </div>
          <ArrowRight size={14} className="text-rd-dim" />
          <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest">
            <Lock size={11} weight="bold" className="text-rd-warn" />
            <span className="text-rd-warn font-bold">no execution</span>
          </div>
        </div>

        {/* Where execution actually lives — for context */}
        <div className="mt-3 pt-3 border-t border-rd-border">
          <div className="text-[10px] text-rd-dim uppercase tracking-widest mb-2">Where execution lives (separate path)</div>
          <div className="flex items-center gap-2 flex-wrap font-mono text-xs">
            <span className="text-rd-alpha font-bold inline-flex items-center gap-1">
              <span className="inline-block w-2 h-2 bg-rd-alpha" /> ALPHA
            </span>
            <span className="text-rd-dim">·</span>
            <span className="text-rd-muted">authority_state ∈ {`{co_trader, primary}`}</span>
            <span className="text-rd-dim">·</span>
            <span className="text-rd-muted">+ Patent J pass</span>
            <span className="text-rd-dim">·</span>
            <span className="text-rd-muted">+ operator countersign</span>
            <span className="text-rd-dim">·</span>
            <span className="text-rd-warn font-bold">currently OBSERVATION ONLY</span>
          </div>
        </div>

        <div className="mt-3 px-3 py-2 border border-rd-border bg-rd-bg2 font-mono text-[10px] text-rd-muted leading-relaxed">
          <Warning size={11} weight="bold" className="inline mr-1 -mt-0.5 text-rd-warn" />
          REDEYE is <span className="text-rd-text font-bold">not</span> a runtime on the trading ladder.
          It does not appear in <code>namespaces.RUNTIMES</code> by design — REDEYE advises Camaro and stops.
          Camaro evaluates and may forward; neither places orders.
        </div>
      </Card>

      {/* The camaro_contract block */}
      <Card className="mb-6" testid="redeye-contract">
        <div className="flex items-baseline justify-between mb-3">
          <div>
            <div className="label-eyebrow">camaro_contract</div>
            <div className="font-mono text-sm">immutable on every payload</div>
          </div>
          <Badge color="#10B981">v1</Badge>
        </div>
        <div className="border border-rd-border overflow-hidden">
          <table className="w-full text-xs font-mono">
            <thead>
              <tr className="bg-rd-bg3 text-rd-dim uppercase tracking-widest">
                <th className="text-left px-3 py-2.5">Field</th>
                <th className="text-left px-3 py-2.5">Required value</th>
                <th className="text-left px-3 py-2.5">Note</th>
              </tr>
            </thead>
            <tbody>
              {CONTRACT_FIELDS.map((row) => (
                <tr key={row.k} className="border-t border-rd-border" data-testid={`contract-${row.k}`}>
                  <td className="px-3 py-2.5 text-rd-text">{row.k}</td>
                  <td className="px-3 py-2.5">
                    <span className="border border-rd-chevelle text-rd-chevelle px-2 py-0.5">{row.v}</span>
                  </td>
                  <td className="px-3 py-2.5 text-rd-muted">{row.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="mt-3 text-[10px] text-rd-dim uppercase tracking-widest">
          Camaro must reject any payload where these values differ.
        </div>
      </Card>

      {/* alpha_alignment forward-compat */}
      <Card className="mb-6" testid="redeye-alignment">
        <div className="label-eyebrow mb-3">alpha_alignment · forward-compat hint</div>
        <p className="text-xs text-rd-muted mb-3 leading-relaxed">
          REDEYE's read on whether this short contradicts Alpha's current long thesis. Always emitted (default null).
          RISEDUALAI's <code className="text-rd-text">_emit_camaro_audit</code> tolerates absence — older REDEYE builds
          land null in the audit row.
        </p>
        <div className="space-y-2">
          {ALPHA_ALIGNMENT_TABLE.map((row) => (
            <div key={row.value} className="border border-rd-border p-3" data-testid={`align-${row.value}`}>
              <div className="flex items-baseline gap-2 mb-1">
                <Badge color={row.badge}>{row.label}</Badge>
                <span className="text-[10px] text-rd-dim uppercase tracking-widest">camaro:</span>
                <span className="text-[11px] font-mono text-rd-muted">{row.camaro}</span>
              </div>
              <div className="text-[11px] text-rd-text">{row.meaning}</div>
            </div>
          ))}
        </div>
      </Card>

      {/* Bridge config */}
      <Card className="mb-6" testid="redeye-config">
        <div className="label-eyebrow mb-3">Bridge thresholds (frozen, REDEYE-side)</div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {BRIDGE_CONFIG.map((row) => (
            <div key={row.k} className="border border-rd-border px-3 py-2 flex items-baseline justify-between font-mono text-xs">
              <div>
                <div className="text-rd-text">{row.k}</div>
                <div className="text-[10px] text-rd-dim uppercase tracking-widest">{row.note}</div>
              </div>
              <span className="text-rd-text" style={{ color: REDEYE_COLOR }}>{row.v}</span>
            </div>
          ))}
        </div>
        <div className="mt-3 text-[10px] text-rd-dim uppercase tracking-widest">
          Source: <code className="text-rd-muted">/app/runtime_patch_kit/redeye/services/redeye_short_bridge.py</code>
        </div>
      </Card>

      {/* Live data placeholder — wire later via Camaro forwarding */}
      <Card className="mb-6" testid="redeye-live-placeholder">
        <div className="flex items-baseline justify-between mb-3">
          <div>
            <div className="label-eyebrow">Live REDEYE feed</div>
            <div className="font-mono text-sm">not yet wired into Mission Control</div>
          </div>
          <Badge color="#A1A1AA">PENDING</Badge>
        </div>
        <div className="border border-dashed border-rd-border bg-rd-bg2 px-4 py-6 text-xs text-rd-muted leading-relaxed font-mono">
          REDEYE does not currently ingest into Mission Control's Mongo. To preserve the
          <span className="text-rd-text"> &quot;REDEYE never bypasses Camaro&quot;</span> doctrine, live data
          would arrive via Camaro forwarding — i.e. Camaro receives REDEYE's payload, validates the
          <code className="text-rd-text"> camaro_contract</code> block, and posts it to a new
          <code className="text-rd-text"> /api/ingest/redeye-via-camaro</code> endpoint. This page renders
          the last 50 such rows once that endpoint is wired.
        </div>
      </Card>

      {/* Cross-references */}
      <Card testid="redeye-references">
        <div className="label-eyebrow mb-3">References</div>
        <div className="space-y-2 text-xs font-mono">
          <ReferenceRow
            icon={Code}
            label="Bridge module (producer)"
            value="/app/runtime_patch_kit/redeye/services/redeye_short_bridge.py"
          />
          <ReferenceRow
            icon={ShieldCheck}
            label="Pulse contract spec"
            value="/app/runtime_patch_kit/redeye/PULSE_CONTRACT.md"
          />
          <ReferenceRow
            icon={Code}
            label="CLI patch instructions"
            value="/app/runtime_patch_kit/redeye/CLI_PATCH.md"
          />
          <div className="pt-2 mt-2 border-t border-rd-border text-[10px] text-rd-dim uppercase tracking-widest">
            Consumer side (audit trail + Pulse card) lives in the <span className="text-rd-text">RISEDUALAI / Camaro</span> repo.
            <Link to="/promotion" className="ml-2 underline text-rd-muted hover:text-rd-text">
              View Camaro authority state →
            </Link>
          </div>
        </div>
      </Card>
    </div>
  );
}

function ReferenceRow({ icon: Icon, label, value }) {
  return (
    <div className="flex items-center gap-3 px-3 py-2 border border-rd-border">
      <Icon size={14} weight="bold" className="text-rd-dim shrink-0" />
      <div className="text-[10px] text-rd-dim uppercase tracking-widest shrink-0">{label}</div>
      <code className="text-rd-text truncate">{value}</code>
    </div>
  );
}
