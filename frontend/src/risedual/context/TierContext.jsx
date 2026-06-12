import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

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
  const setTier = useCallback((t) => {
    setTierState(t);
    try {
      localStorage.setItem(STORAGE_KEY, t);
    } catch (e) {
      // Storage disabled (private mode / sandboxed iframe); fall back to in-memory only.
      console.debug("TierContext: localStorage write failed", e);
    }
  }, []);
  useEffect(() => {
    if (!tier) setTier("free");
  }, [tier, setTier]);
  const value = useMemo(() => ({ tier, setTier }), [tier, setTier]);
  return (
    <TierContext.Provider value={value}>
      {children}
    </TierContext.Provider>
  );
}

export function useTier() {
  const ctx = useContext(TierContext);
  if (!ctx) throw new Error("useTier must be used inside <TierProvider>");
  return ctx;
}
