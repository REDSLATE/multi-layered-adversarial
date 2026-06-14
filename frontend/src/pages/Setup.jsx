import React from "react";
import { PageHeader } from "@/components/ui-bits";
import PanelErrorBoundary from "@/components/PanelErrorBoundary";
import RuntimeBundlesPanel from "@/components/RuntimeBundlesPanel";
import RuntimeTokensPanel from "@/components/RuntimeTokensPanel";

/**
 * Setup — operator-rare admin actions. Moved here (2026-02-19) off the
 * Diagnostics page so the day-to-day health surface stays scannable.
 *
 * Lives:
 *   - Portable patch kits (.tar.gz / .zip downloads for sidecar repos)
 *   - Brain ingest token reveal + .env snippet generators
 *
 * Both are operator-initiated actions touched on the order of weeks,
 * not minutes. Mounting them on Diagnostics was burning ~2 extra
 * fetches per page load for zero day-to-day value.
 */
export default function SetupPage() {
  return (
    <div className="space-y-4" data-testid="setup-page">
      <PageHeader
        eyebrow="Shared · Setup"
        title="Operator setup"
        sub="Sidecar patch kits and brain ingest token rotation. Operator-rare; lives here so the Diagnostics page stays focused on real-time health."
        testid="setup-header"
      />

      <PanelErrorBoundary panelName="RuntimeBundlesPanel">
        <RuntimeBundlesPanel />
      </PanelErrorBoundary>

      <PanelErrorBoundary panelName="RuntimeTokensPanel">
        <RuntimeTokensPanel />
      </PanelErrorBoundary>
    </div>
  );
}
