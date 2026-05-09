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
import RuntimeDetail from "@/pages/RuntimeDetail";
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
            <Route path="runtime/:runtime" element={<RuntimeDetail />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}

export default App;
