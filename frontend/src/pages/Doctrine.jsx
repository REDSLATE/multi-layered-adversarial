import React, { useState } from "react";
import { PageHeader } from "@/components/ui-bits";
import DoctrineHealthPanel from "@/components/DoctrineHealthPanel";
import PanelErrorBoundary from "@/components/PanelErrorBoundary";
import { Shield } from "@phosphor-icons/react";

const LANES = ["all", "equity", "crypto"];

function LanePill({ value, current, onClick }) {
  const active = value === current;
  return (
    <button
      onClick={() => onClick(value)}
      data-testid={`doctrine-lane-${value}`}
      className="px-3 py-1 text-[11px] font-mono uppercase tracking-wider border transition-colors"
      style={{
        borderColor: active ? "var(--rd-text)" : "var(--rd-border)",
        color: active ? "var(--rd-text)" : "var(--rd-dim)",
        background: active ? "var(--rd-bg2)" : "transparent",
      }}
    >
      {value}
    </button>
  );
}

export default function Doctrine() {
  const [lane, setLane] = useState("all");

  return (
    <div data-testid="doctrine-page">
      <PageHeader
        icon={Shield}
        title="Doctrine Health"
        subtitle={
          "Live operational state of every doctrine version. Expectancy-driven "
          + "promotion gate, read-only. Promotion / retirement targets "
          + "(lane, seat, doctrine_version) — never brain identity."
        }
        testid="doctrine-header"
      />

      <div className="mb-4 flex items-center gap-2">
        <span className="text-[10px] font-mono uppercase tracking-widest text-rd-dim">
          Lane
        </span>
        {LANES.map((l) => (
          <LanePill key={l} value={l} current={lane} onClick={setLane} />
        ))}
      </div>

      <PanelErrorBoundary panelName="Doctrine Health" testid="panel-error-doctrine-health-full">
        <DoctrineHealthPanel mode="full" lane={lane} />
      </PanelErrorBoundary>

      <div
        className="mt-6 px-4 py-3 border border-rd-border bg-rd-bg2 text-[10px] font-mono text-rd-dim leading-relaxed"
        data-testid="doctrine-gate-thresholds"
      >
        <div className="text-rd-text uppercase tracking-widest mb-1.5">
          Gate Thresholds (pinned)
        </div>
        <div>min_samples ≥ 100 · expectancy_promotion_floor ≥ +0.30R · max_drawdown_promotion_ceiling ≤ 5R · consistency_promotion_floor ≥ 0.55</div>
        <div>expectancy_retirement_floor &lt; -0.10R · max_drawdown_retirement_floor ≥ 8R</div>
        <div className="mt-2 italic text-rd-muted">
          Read-only gate state. Operators promote / retire doctrines explicitly. Expectancy is the headline metric — accuracy alone is a trap (45% × 4.5R outperforms 75% × 0.8R).
        </div>
      </div>
    </div>
  );
}
