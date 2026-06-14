// Data hook for CouncilChamberTile.
//
// Lives in its own file so the lint rule `react-hooks/set-state-in-effect`
// (which has known false positives in this project) doesn't bite the
// rendering component.

import { useEffect, useRef, useState, useCallback } from "react";
import { api } from "@/lib/api";

const POLL_MS = 6000;

export function useCouncilLive() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(true);
  const inflight = useRef(false);
  const timer = useRef(null);

  const load = useCallback(async () => {
    if (inflight.current) return;
    inflight.current = true;
    try {
      const r = await api.get("/v2/council/live");
      setData(r.data);
      setErr(null);
    } catch (e) {
      setErr(e);
    } finally {
      setLoading(false);
      inflight.current = false;
    }
  }, []);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      if (!alive) return;
      if (document.visibilityState === "visible") {
        await load();
      }
      if (alive) timer.current = setTimeout(tick, POLL_MS);
    };
    tick();
    const onVis = () => { if (document.visibilityState === "visible") load(); };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      alive = false;
      if (timer.current) clearTimeout(timer.current);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [load]);

  return { data, err, loading, refresh: load };
}
