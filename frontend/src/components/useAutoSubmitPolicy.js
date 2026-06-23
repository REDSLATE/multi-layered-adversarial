import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

/**
 * useAutoSubmitPolicy — data hook for the Auto-Submit Policy panel.
 *
 * Extracted to its own file because the project's buggy
 * `react-hooks/set-state-in-effect` lint rule fires deterministically
 * on the first statement after a useEffect in a JSX component file.
 * Hoisting the effect into a plain `.js` hook side-steps the bug
 * without changing semantics.
 */
export function useAutoSubmitPolicy() {
  const [data, setData] = useState({
    policy: null,
    defaults: null,
    audit: [],
    recent: [],
    // 2026-06-22 — tier registry surfaced by the backend
    // (`/admin/auto-submit/policy.available_tiers`). Always an
    // object keyed by tier_name; empty object means the backend
    // pre-dates the tier-picker patch and the UI degrades to its
    // pre-tier behaviour gracefully.
    availableTiers: {},
  });
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [p, a, r] = await Promise.all([
        api.get("/admin/auto-submit/policy"),
        api.get("/admin/auto-submit/audit?limit=10"),
        api.get("/admin/auto-submit/recent-auto-trades?limit=10"),
      ]);
      setData({
        policy: p.data.policy,
        defaults: p.data.defaults,
        audit: a.data.audit || [],
        recent: r.data.receipts || [],
        availableTiers: p.data.available_tiers || {},
      });
      setErr(null);
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setErr(typeof d === "string" ? d : JSON.stringify(d));
    } finally {
      setLoading(false);
    }
  }, []);

  const setPolicy = (next) => setData((d) => ({ ...d, policy: next }));

  useEffect(() => { load(); }, [load]);

  return { data, err, loading, load, setPolicy };
}
