import React, { createContext, useContext, useState, useEffect } from "react";

const TierContext = createContext(null);

const STORAGE_KEY = "risedual_site_tier";

export function TierProvider({ children }) {
  const [tier, setTierState] = useState(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) || "free";
    } catch {
      return "free";
    }
  });
  const setTier = (t) => {
    setTierState(t);
    try {
      localStorage.setItem(STORAGE_KEY, t);
    } catch {}
  };
  useEffect(() => {
    if (!tier) setTier("free");
  }, [tier]);
  return (
    <TierContext.Provider value={{ tier, setTier }}>
      {children}
    </TierContext.Provider>
  );
}

export function useTier() {
  const ctx = useContext(TierContext);
  if (!ctx) throw new Error("useTier must be used inside <TierProvider>");
  return ctx;
}
