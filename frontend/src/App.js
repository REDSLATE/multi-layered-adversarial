import React, { useEffect, useState } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "@/context/AuthContext";
import { computeHostRedirect } from "@/lib/hostPolicy";
import Login from "@/pages/Login";
import Layout from "@/components/Layout";
import Overview from "@/pages/Overview";
import Receipts from "@/pages/Receipts";
import MemoryFirewall from "@/pages/MemoryFirewall";
import Calibration from "@/pages/Calibration";
import FeatureBuilders from "@/pages/FeatureBuilders";
import Artifacts from "@/pages/Artifacts";
import Diagnostics from "@/pages/Diagnostics";
import Flags from "@/pages/Flags";
// "Promotion" (Patent G + J governance) page removed from routing
// 2026-02-19 — superseded by Paradox V2 seat-policy + 25-eval
// autonomy ladder. The /pages/Promotion.jsx file is left on disk
// for git history but no longer imported or routable.
import RecentIngests from "@/pages/RecentIngests";
import RuntimeDetail from "@/pages/RuntimeDetail";
import BrainConsole from "@/pages/BrainConsole";
// 2026-07-01 (Pass 2/3 cleanup, batch 3): LearningLadder removed —
// hypothesis→paper→live promotion ladder was an auto-router-era
// concept. The sidecar trader has no promotion ladder; it fires
// or holds. Backend queries also hit Atlas and SSL-timed-out on
// the shared-tier connection.
import Intents from "@/pages/Intents";
import Witnesses from "@/pages/Witnesses";  // 2026-02-23 witness-council read-only panel
import SeatContext from "@/pages/SeatContext";  // 2026-02-23 cleaned witness context for the Seat
import SetupPage from "@/pages/Setup";
import Hypothesis from "@/pages/Hypothesis";
import McShelly from "@/pages/McShelly";
import Redeye from "@/pages/Redeye";
import BrainOperatorPage from "@/pages/BrainOperatorPage";
import Discussion from "@/pages/Discussion";
import Scorecards from "@/pages/Scorecards";
import Conflicts from "@/pages/Conflicts";
import Doctrine from "@/pages/Doctrine";
import DoctrineReference from "@/pages/DoctrineReference";
import SafetyGatesAudit from "@/pages/SafetyGatesAudit";
import Positions from "@/pages/Positions";
import PublicTraffic from "@/pages/PublicTraffic";
import LlmLedger from "@/pages/LlmLedger";
import RiseAI from "@/pages/RiseAI";
import Ping from "@/pages/Ping";
import RisedualLayout from "@/risedual/Layout";
import RdLanding from "@/risedual/pages/Landing";
import RdSignals from "@/risedual/pages/Signals";
import RdSignalDetail from "@/risedual/pages/SignalDetail";
import RdDigest from "@/risedual/pages/Digest";
import RdChat from "@/risedual/pages/Chat";
import RdScanner from "@/risedual/pages/Scanner";
import RdHeatmap from "@/risedual/pages/Heatmap";
import RdAgentActivity from "@/risedual/pages/AgentActivity";
import RdMarkets from "@/risedual/pages/Markets";
import "@/App.css";

function Protected({ children }) {
  const { user, status } = useAuth();
  if (status === "loading") {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[#0A0A0A] text-zinc-400 font-mono text-xs uppercase tracking-[0.3em]">
        Authenticating
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace />;
  return children;
}

function HostGuard({ children }) {
  // Decide once on mount. If the hostname says we're on the wrong surface,
  // window.location.replace ourselves — never render the misplaced bundle.
  const [redirecting, setRedirecting] = useState(() => !!computeHostRedirect());

  useEffect(() => {
    const target = computeHostRedirect();
    if (target) {
      setRedirecting(true);
      window.location.replace(target);
    }
  }, []);

  if (redirecting) {
    return (
      <div
        className="min-h-screen flex items-center justify-center bg-[#0F172A] text-zinc-400 font-mono text-xs uppercase tracking-[0.3em]"
        data-testid="host-redirect"
      >
        Redirecting…
      </div>
    );
  }
  return children;
}

function App() {
  return (
    <HostGuard>
      <AuthProvider>
        <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/ping/:brain" element={<Ping />} />

          {/* Public site at root (was /r before the 2026-02-13 swap) */}
          <Route path="/" element={<RisedualLayout />}>
            <Route index element={<RdLanding />} />
            <Route path="signals" element={<RdSignals />} />
            <Route path="signals/:id" element={<RdSignalDetail />} />
            <Route path="markets" element={<RdMarkets />} />
            <Route path="digest" element={<RdDigest />} />
            <Route path="chat" element={<RdChat />} />
            <Route path="scanner" element={<RdScanner />} />
            <Route path="heatmap" element={<RdHeatmap />} />
            <Route path="activity" element={<RdAgentActivity />} />
          </Route>

          {/* Legacy /r/* — redirect to root for any bookmarked URL */}
          <Route path="/r" element={<Navigate to="/" replace />} />
          <Route path="/r/*" element={<Navigate to="/" replace />} />

          {/* Operator dashboard moved to /admin/* */}
          <Route
            path="/admin"
            element={
              <Protected>
                <Layout />
              </Protected>
            }
          >
            <Route index element={<Navigate to="/admin/hypothesis" replace />} />
            <Route path="overview" element={<Overview />} />
            <Route path="receipts" element={<Receipts />} />
            <Route path="memory" element={<MemoryFirewall />} />
            <Route path="calibration" element={<Calibration />} />
            <Route path="feature-builders" element={<FeatureBuilders />} />
            <Route path="artifacts" element={<Artifacts />} />
            <Route path="diagnostics" element={<Diagnostics />} />
            <Route path="flags" element={<Flags />} />
            <Route path="recent" element={<RecentIngests />} />
            <Route path="runtime/:runtime" element={<RuntimeDetail />} />
            <Route path="brain/:brain" element={<BrainConsole />} />
            <Route path="intents" element={<Intents />} />
            <Route path="witnesses" element={<Witnesses />} />
            <Route path="seat-context" element={<SeatContext />} />
            <Route path="setup" element={<SetupPage />} />
            <Route path="hypothesis" element={<Hypothesis />} />
            <Route path="mc-shelly" element={<McShelly />} />
            <Route path="gto" element={<Redeye />} />
            <Route path="brain-op/:brain" element={<BrainOperatorPage />} />
            <Route path="discussion" element={<Discussion />} />
            <Route path="scorecards" element={<Scorecards />} />
            <Route path="doctrine" element={<Doctrine />} />
            <Route path="doctrine-reference" element={<DoctrineReference />} />
            <Route path="safety-gates" element={<SafetyGatesAudit />} />
            <Route path="conflicts" element={<Conflicts />} />
            <Route path="positions" element={<Positions />} />
            <Route path="public-traffic" element={<PublicTraffic />} />
            <Route path="llm-ledger" element={<LlmLedger />} />
            <Route path="rise-ai" element={<RiseAI />} />
          </Route>

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
    </HostGuard>
  );
}

export default App;
