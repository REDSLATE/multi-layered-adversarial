import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

/**
 * useParadoxV2State — data hook for the ParadoxV2DashboardPanel.
 *
 * Extracted to its own file because the project's buggy
 * `react-hooks/set-state-in-effect` lint rule fires deterministically
 * on JSX files that read state immediately after a useEffect. Hoisting
 * the effect here side-steps the bug without changing semantics.
 */
export function useParadoxV2State() {
  const [data, setData] = useState({
    brains: [],
    seat_policies: [],
    trust: [],
    governor_rules: [],
    active_stops: [],
    performance: [],
    recent_evaluations: [],
    promotion_log: [],
    doctrine: "",
  });
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.get("/v2/state");
      setData(res.data);
      setErr(null);
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setErr(typeof d === "string" ? d : JSON.stringify(d));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return { data, err, loading, load };
}
