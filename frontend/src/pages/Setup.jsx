import React from "react";
import { PageHeader } from "@/components/ui-bits";
import PanelErrorBoundary from "@/components/PanelErrorBoundary";
import RuntimeBundlesPanel from "@/components/RuntimeBundlesPanel";

/**
 * Setup — operator-rare admin actions. Moved here (2026-02-19) off the
 * Diagnostics page so the day-to-day health surface stays scannable.
 *
 * Lives:
 *   - Portable patch kits (.tar.gz / .zip downloads for sidecar repos)
 *
 * RuntimeTokensPanel removed 2026-07-01: the `/admin/runtime-tokens`
 * endpoints were deleted in Pass 2/3. Brain ingest tokens are no
 * longer rotated through MC — brains run in-process now.
 */
export default function SetupPage() {
  return (
    <div className="space-y-4" data-testid="setup-page">
      <PageHeader
        eyebrow="Shared · Setup"
        title="Operator setup"
        sub="Sidecar patch kits. Operator-rare; lives here so the Diagnostics page stays focused on real-time health."
        testid="setup-header"
      />

      <PanelErrorBoundary panelName="RuntimeBundlesPanel">
        <RuntimeBundlesPanel />
      </PanelErrorBoundary>
    </div>
  );
}
