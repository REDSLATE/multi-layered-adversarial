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
import Promotion from "@/pages/Promotion";
import RecentIngests from "@/pages/RecentIngests";
import RuntimeDetail from "@/pages/RuntimeDetail";
import BrainConsole from "@/pages/BrainConsole";
import Intents from "@/pages/Intents";
import Hypothesis from "@/pages/Hypothesis";
import McShelly from "@/pages/McShelly";
import Redeye from "@/pages/Redeye";
import Discussion from "@/pages/Discussion";
import Scorecards from "@/pages/Scorecards";
import Conflicts from "@/pages/Conflicts";
import Positions from "@/pages/Positions";
import PublicTraffic from "@/pages/PublicTraffic";
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
            <Route path="promotion" element={<Promotion />} />
            <Route path="recent" element={<RecentIngests />} />
            <Route path="runtime/:runtime" element={<RuntimeDetail />} />
            <Route path="brain/:brain" element={<BrainConsole />} />
            <Route path="intents" element={<Intents />} />
            <Route path="hypothesis" element={<Hypothesis />} />
            <Route path="mc-shelly" element={<McShelly />} />
            <Route path="redeye" element={<Redeye />} />
            <Route path="discussion" element={<Discussion />} />
            <Route path="scorecards" element={<Scorecards />} />
            <Route path="conflicts" element={<Conflicts />} />
            <Route path="positions" element={<Positions />} />
            <Route path="public-traffic" element={<PublicTraffic />} />
          </Route>

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
    </HostGuard>
  );
}

export default App;
