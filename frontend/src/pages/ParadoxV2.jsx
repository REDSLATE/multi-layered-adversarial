import React from "react";
import { PageHeader, Card } from "@/components/ui-bits";
import CouncilChamberTile from "@/components/CouncilChamberTile";
import ParadoxV2DashboardPanel from "@/components/ParadoxV2DashboardPanel";
import PanelErrorBoundary from "@/components/PanelErrorBoundary";

/**
 * Paradox V2 — dedicated operator surface for the seat-owned execution
 * doctrine. Lives at /admin/paradox. Two stacked panels:
 *
 *   1. Council Chamber — real-time 4-column view of each canonical
 *      brain's latest BrainVote. Polls every 6 s. Quorum indicator
 *      shows how many brains have spoken in the last 10 min.
 *   2. Paradox V2 Dashboard — seats × instruments table, promotion
 *      readiness strip (25-eval operator-driven gate), trust list,
 *      active RoadGuard stops, governor rules, inline /v2/evaluate
 *      test-fire, recent receipts, promotion log.
 *
 * Moved off /admin/intents (2026-02-19) — Intents page was getting
 * cluttered and these surfaces are doctrine-specific.
 */
export default function ParadoxV2Page() {
  return (
    <div className="space-y-4" data-testid="paradox-v2-page">
      <PageHeader
        title="Seraph"
        kicker="Decision Doctrine"
        subtitle="Seat-owned execution. Brain owns doctrine. Seat owns execution. Governor owns modifiers. RoadGuard owns stops. Verifier owns promotion."
      />

      <PanelErrorBoundary label="Council Chamber">
        <CouncilChamberTile />
      </PanelErrorBoundary>

      <PanelErrorBoundary label="Seraph Dashboard">
        <ParadoxV2DashboardPanel />
      </PanelErrorBoundary>
    </div>
  );
}
