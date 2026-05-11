import React, { useEffect, useState, useCallback } from "react";
import { api, fmtTime, relTime } from "@/lib/api";
import { PageHeader, Card, Badge, EmptyState, LoadingRow } from "@/components/ui-bits";
import { ChatCircleDots, ArrowBendUpRight, Lock, MagnifyingGlass } from "@phosphor-icons/react";
import AuditReplay from "@/components/AuditReplay";

// Brain colours — must match Layout.jsx + RUNTIME_META.
const BRAIN_META = {
  alpha:    { label: "ALPHA",    color: "#3B82F6" },
  camaro:   { label: "CAMARO",   color: "#F59E0B" },
  chevelle: { label: "CHEVELLE", color: "#10B981" },
  redeye:   { label: "REDEYE",   color: "#DC2626" },
};

const STANCE_META = {
  long:        { color: "#3B82F6" },
  short:       { color: "#DC2626" },
  veto:        { color: "#EF4444" },
  endorse:     { color: "#10B981" },
  question:    { color: "#A1A1AA" },
  observation: { color: "#FBBF24" },
};

export default function Discussion() {
  const [items, setItems] = useState(null);
  const [filter, setFilter] = useState({ runtime: "", symbol: "", thread: "" });
  const [err, setErr] = useState("");

  const refresh = useCallback(async () => {
    setErr("");
    try {
      const params = new URLSearchParams();
      params.set("limit", "200");
      if (filter.runtime) params.set("runtime", filter.runtime);
      if (filter.symbol)  params.set("symbol", filter.symbol.trim().toUpperCase());
      if (filter.thread)  params.set("thread", filter.thread.trim());
      const { data } = await api.get(`/shared/opinions?${params.toString()}`);
      setItems(data.items);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  }, [filter]);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    const id = setInterval(refresh, 7000);  // poll every 7s — pull-only by design
    return () => clearInterval(id);
  }, [refresh]);

  // Group by thread_root, oldest-first within a thread.
  const threads = React.useMemo(() => {
    if (!items) return null;
    const grouped = new Map();
    for (const x of items) {
      const root = x.thread_root || x.opinion_id;
      if (!grouped.has(root)) grouped.set(root, []);
      grouped.get(root).push(x);
    }
    const arr = [...grouped.entries()].map(([root, msgs]) => ({
      root,
      msgs: [...msgs].sort((a, b) => a.posted_at.localeCompare(b.posted_at)),
    }));
    // Sort threads by latest activity, newest first.
    arr.sort((a, b) => {
      const aLast = a.msgs[a.msgs.length - 1].posted_at;
      const bLast = b.msgs[b.msgs.length - 1].posted_at;
      return bLast.localeCompare(aLast);
    });
    return arr;
  }, [items]);

  return (
    <div className="reveal" data-testid="discussion-page">
      <PageHeader
        eyebrow="Cross-brain · mediated"
        title="Discussion"
        sub="Brains share opinions, not internal model state. All comms mediated through Mission Control. Pull-only consumption. None can execute, paper or live — schema-enforced."
        right={
          <div className="flex items-center gap-2">
            <Badge color="#FBBF24">
              <Lock size={11} weight="bold" className="inline mr-1 -mt-0.5" />
              ADMIN ONLY
            </Badge>
            <Badge color="#10B981">PULL-ONLY</Badge>
          </div>
        }
        testid="discussion-header"
      />

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-4 text-xs font-mono">
          {err}
        </div>
      )}

      {/* Filters */}
      <Card className="mb-4" testid="discussion-filters">
        <div className="grid grid-cols-1 md:grid-cols-4 gap-3 items-end">
          <div>
            <div className="label-eyebrow mb-1">Runtime</div>
            <select
              value={filter.runtime}
              onChange={(e) => setFilter((f) => ({ ...f, runtime: e.target.value }))}
              className="bg-rd-bg2 border border-rd-border text-rd-text font-mono text-xs px-3 py-2 w-full"
              data-testid="discussion-filter-runtime"
            >
              <option value="">all brains</option>
              {Object.keys(BRAIN_META).map((k) => (
                <option key={k} value={k}>{BRAIN_META[k].label}</option>
              ))}
            </select>
          </div>
          <div>
            <div className="label-eyebrow mb-1">Symbol</div>
            <input
              value={filter.symbol}
              onChange={(e) => setFilter((f) => ({ ...f, symbol: e.target.value }))}
              placeholder="TSLA"
              className="bg-rd-bg2 border border-rd-border text-rd-text font-mono text-xs px-3 py-2 w-full uppercase"
              data-testid="discussion-filter-symbol"
            />
          </div>
          <div className="md:col-span-2">
            <div className="label-eyebrow mb-1">Thread root (opinion_id)</div>
            <div className="flex gap-2">
              <input
                value={filter.thread}
                onChange={(e) => setFilter((f) => ({ ...f, thread: e.target.value }))}
                placeholder="paste opinion_id to filter to thread"
                className="bg-rd-bg2 border border-rd-border text-rd-text font-mono text-xs px-3 py-2 flex-1"
                data-testid="discussion-filter-thread"
              />
              <button
                onClick={() => setFilter({ runtime: "", symbol: "", thread: "" })}
                className="btn-sharp px-3 py-2 border border-rd-border text-rd-muted hover:text-rd-text"
                data-testid="discussion-filter-clear"
              >
                clear
              </button>
            </div>
          </div>
        </div>
      </Card>

      {!threads && <LoadingRow />}
      {threads && threads.length === 0 && (
        <EmptyState
          message="No opinions match these filters. Try removing the symbol or runtime filter, or wait for a brain to post."
          testid="discussion-empty"
        />
      )}

      {threads && threads.length > 0 && (
        <div className="space-y-4" data-testid="discussion-threads">
          {threads.map((t) => (
            <Card key={t.root} className="p-0 overflow-hidden" testid={`thread-${t.root}`}>
              <div className="px-4 py-3 border-b border-rd-border flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <ChatCircleDots size={14} weight="bold" className="text-rd-warn" />
                  <div className="font-mono text-xs text-rd-muted">
                    thread · <span className="text-rd-text">{t.msgs[0].topic}</span>
                  </div>
                </div>
                <div className="text-[10px] text-rd-dim uppercase tracking-widest">
                  {t.msgs.length} message{t.msgs.length === 1 ? "" : "s"} · last {relTime(t.msgs[t.msgs.length - 1].posted_at)}
                </div>
              </div>

              <div className="divide-y divide-rd-border">
                {t.msgs.map((m, idx) => {
                  const meta = BRAIN_META[m.runtime] || { label: m.runtime.toUpperCase(), color: "#A1A1AA" };
                  const stance = STANCE_META[m.stance] || { color: "#A1A1AA" };
                  return (
                    <div
                      key={m.opinion_id}
                      className="px-4 py-3 hover:bg-rd-bg3"
                      style={{ paddingLeft: `${16 + Math.min(m.depth, 6) * 16}px` }}
                      data-testid={`opinion-${m.opinion_id}`}
                    >
                      <div className="flex items-baseline gap-2 flex-wrap">
                        {idx > 0 && (
                          <ArrowBendUpRight size={11} weight="bold" className="text-rd-dim mt-1" />
                        )}
                        <span className="font-mono font-bold text-xs" style={{ color: meta.color }}>
                          {meta.label}
                        </span>
                        <Badge color={stance.color}>{m.stance}</Badge>
                        <span className="text-[10px] font-mono text-rd-dim">
                          confidence {Number(m.confidence).toFixed(2)}
                        </span>
                        <span className="text-[10px] font-mono text-rd-dim ml-auto" title={fmtTime(m.posted_at)}>
                          {relTime(m.posted_at)}
                        </span>
                      </div>
                      <div className="mt-1.5 text-xs font-mono text-rd-text whitespace-pre-wrap leading-relaxed">
                        {m.body}
                      </div>
                      {m.evidence?.technical_ref && (
                        <AuditReplay
                          technicalRef={m.evidence.technical_ref}
                          quotedValues={m.evidence.values}
                        />
                      )}
                      {m.evidence && Object.keys(m.evidence).length > 0 && (
                        <details className="mt-2">
                          <summary className="text-[10px] uppercase tracking-widest text-rd-dim cursor-pointer hover:text-rd-muted flex items-center gap-1">
                            <MagnifyingGlass size={10} weight="bold" />
                            evidence
                          </summary>
                          <pre className="mt-1.5 px-2 py-2 bg-rd-bg2 border border-rd-border text-[10px] font-mono text-rd-muted overflow-x-auto">
{JSON.stringify(m.evidence, null, 2)}
                          </pre>
                        </details>
                      )}
                    </div>
                  );
                })}
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
