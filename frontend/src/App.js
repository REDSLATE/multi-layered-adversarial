import React, { useEffect, useState } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "@/context/AuthContext";
import { computeHostRedirect } from "@/lib/hostPolicy";
import Login from "@/pages/Login";
import Layout from "@/components/Layout";
import Overview from "@/pages/Overview";
import Receipts from "@/pages/Receipts";
import Diagnostics from "@/pages/Diagnostics";
import Flags from "@/pages/Flags";
import RecentIngests from "@/pages/RecentIngests";
import RuntimeDetail from "@/pages/RuntimeDetail";
import BrainConsole from "@/pages/BrainConsole";
import BrainOperatorPage from "@/pages/BrainOperatorPage";
import Intents from "@/pages/Intents";
import McShelly from "@/pages/McShelly";
import DoctrineReference from "@/pages/DoctrineReference";
import Architecture from "@/pages/Architecture";
import Positions from "@/pages/Positions";
import LlmLedger from "@/pages/LlmLedger";
import RiseAI from "@/pages/RiseAI";
import KernelReview from "@/pages/KernelReview";
import PulseHealth from "@/pages/PulseHealth";
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
  if (status === "loading") return null;
  if (!user) return <Navigate to="/login" replace />;
  return children;
}

// Host-based redirect: only used for the very first paint. After that
// the SPA takes over normal routing.
function HostGuard({ children }) {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const target = computeHostRedirect(window.location);
    if (target) {
      window.location.replace(target);
      return;
    }
    setReady(true);
  }, []);
  if (!ready) return null;
  return children;
}

function App() {
  return (
    <HostGuard>
      <AuthProvider>
        <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />

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

          {/* Operator dashboard */}
          <Route
            path="/admin"
            element={
              <Protected>
                <Layout />
              </Protected>
            }
          >
            <Route index element={<Navigate to="/admin/overview" replace />} />
            <Route path="overview" element={<Overview />} />
            <Route path="positions" element={<Positions />} />
            <Route path="intents" element={<Intents />} />
            <Route path="receipts" element={<Receipts />} />
            <Route path="kernel-review" element={<KernelReview />} />
            <Route path="pulse-health" element={<PulseHealth />} />
            <Route path="brain/:brain" element={<BrainConsole />} />
            <Route path="brain-op/:brain" element={<BrainOperatorPage />} />
            <Route path="runtime/:runtime" element={<RuntimeDetail />} />
            <Route path="doctrine-reference" element={<DoctrineReference />} />
            <Route path="architecture" element={<Architecture />} />
            <Route path="flags" element={<Flags />} />
            <Route path="diagnostics" element={<Diagnostics />} />
            <Route path="recent" element={<RecentIngests />} />
            <Route path="mc-shelly" element={<McShelly />} />
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
