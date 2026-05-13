import React from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "@/context/AuthContext";
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

function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/ping/:brain" element={<Ping />} />
          <Route path="/r" element={<RisedualLayout />}>
            <Route index element={<RdLanding />} />
            <Route path="signals" element={<RdSignals />} />
            <Route path="signals/:id" element={<RdSignalDetail />} />
            <Route path="digest" element={<RdDigest />} />
            <Route path="chat" element={<RdChat />} />
            <Route path="scanner" element={<RdScanner />} />
            <Route path="heatmap" element={<RdHeatmap />} />
            <Route path="activity" element={<RdAgentActivity />} />
          </Route>
          <Route
            path="/"
            element={
              <Protected>
                <Layout />
              </Protected>
            }
          >
            <Route index element={<Overview />} />
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
  );
}

export default App;
