import React, { useCallback, useEffect, useState } from "react";
import { api, relTime } from "@/lib/api";
import { Card } from "@/components/ui-bits";

export default function AtlasOutboxTile() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    try {
      const { data: d } = await api.get("/admin/hotpath/outbox");
      setData(d);
      setErr(null);
    } catch (e) {
      setErr(e?.response?.data?.detail || String(e));
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  const act = async (path) => {
    setBusy(true);
    try {
      await api.post(`/admin/hotpath/outbox/${path}`);
      await load();
    } catch (e) {
      setErr(e?.response?.data?.detail || String(e));
    } finally {
      setBusy(false);
    }
  };

  const s = data?.status || {};
  const dead = data?.dead_letters || [];
  return (
    <Card className="p-4 mt-6" testid="atlas-outbox-tile">
      <div className="flex items-center justify-between mb-2">
        <div className="label-eyebrow text-rd-dim">
          Atlas Outbox · durable write-behind (SQLite)
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => act("drain")} disabled={busy}
            data-testid="outbox-drain-btn"
            className="text-[9px] font-mono uppercase tracking-widest border border-rd-border hover:border-rd-text px-2 py-0.5 disabled:opacity-40">
            drain now
          </button>
          {(s.dead_letter ?? 0) > 0 && (
            <button onClick={() => act("retry-dead")} disabled={busy}
              data-testid="outbox-retry-dead-btn"
              className="text-[9px] font-mono uppercase tracking-widest border border-rd-danger text-rd-danger px-2 py-0.5 disabled:opacity-40">
              retry dead
            </button>
          )}
        </div>
      </div>

      {err && (
        <div className="text-[10px] font-mono text-rd-danger mb-2" data-testid="outbox-error">{err}</div>
      )}

      <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 text-[10px] font-mono">
        <div data-testid="outbox-pending">
          <div className="text-rd-dim uppercase text-[8px] tracking-widest">pending</div>
          <div className={`font-bold text-[13px] ${(s.pending ?? 0) > 50 ? "text-amber-500" : "text-rd-text"}`}>{s.pending ?? "—"}</div>
        </div>
        <div data-testid="outbox-oldest">
          <div className="text-rd-dim uppercase text-[8px] tracking-widest">oldest pending</div>
          <div className="text-rd-text">{s.oldest_pending_at ? relTime(s.oldest_pending_at) : "—"}</div>
        </div>
        <div data-testid="outbox-dead">
          <div className="text-rd-dim uppercase text-[8px] tracking-widest">dead letters</div>
          <div className={`font-bold text-[13px] ${(s.dead_letter ?? 0) > 0 ? "text-rd-danger" : "text-rd-text"}`}>{s.dead_letter ?? "—"}</div>
        </div>
        <div data-testid="outbox-acked">
          <div className="text-rd-dim uppercase text-[8px] tracking-widest">atlas acked</div>
          <div className="text-emerald-500 font-bold text-[13px]">{s.acked_total ?? "—"}</div>
        </div>
        <div data-testid="outbox-writer">
          <div className="text-rd-dim uppercase text-[8px] tracking-widest">writer</div>
          <div className={s.writer?.running ? "text-emerald-500" : "text-rd-danger"}>
            {s.writer?.running ? `alive · ${Math.round(s.writer.interval_sec)}s` : "STOPPED"}
          </div>
        </div>
      </div>

      {s.last_error && (
        <div className="text-[9px] font-mono text-amber-500 mt-2 truncate" data-testid="outbox-last-error">
          last error: {s.last_error}
        </div>
      )}

      {dead.length > 0 && (
        <div className="mt-2 border-t border-rd-border/50 pt-1" data-testid="outbox-dead-list">
          {dead.slice(0, 5).map((d) => (
            <div key={d.id} className="text-[9px] font-mono text-rd-dim truncate">
              {d.event_type} · {d.aggregate_id} · {d.attempt_count} attempts · {d.last_error}
            </div>
          ))}
        </div>
      )}

      <div className="text-[10px] text-rd-muted mt-2 font-mono leading-relaxed">
        Exit outcomes and permanent receipts commit locally first; Atlas receives them asynchronously with retry.
        An Atlas outage cannot drop learning records or expectancy rows.
      </div>
    </Card>
  );
}
