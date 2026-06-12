import React, { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { Card, Badge } from "@/components/ui-bits";
import {
  Brain, PaperPlaneTilt, ArrowSquareOut, ThumbsUp, ThumbsDown, Minus,
  Shield, Lightning, Eye, FloppyDisk, Plus, MagnifyingGlass, PushPin,
  Archive as ArchiveIcon, ArrowsClockwise,
} from "@phosphor-icons/react";

/**
 * /admin/rise-ai — Operator console for RISE_AI's cognition layer.
 *
 * Doctrine pin:
 *   This is NOT "ChatGPT for trading." It is the operator's
 *   front-door to the LLM kernel + Paradox + Memory surfaces.
 *
 *   Every interaction lands in the ledger (`/admin/llm-ledger`)
 *   and is gradable inline — every conversation becomes training
 *   data. No broker routes, no execution endpoints, no admin
 *   mutation tools are reachable from this UI (enforced at the
 *   `/api/ai/run` API layer).
 */

const MODES = [
  { value: "chat",     label: "CHAT",     icon: Brain,     desc: "Conversational" },
  { value: "reason",   label: "REASON",   icon: Lightning, desc: "Structured reasoning" },
  { value: "research", label: "RESEARCH", icon: Eye,       desc: "Synthesize known" },
  { value: "code",     label: "CODE",     icon: Brain,     desc: "Code assist" },
  { value: "memory",   label: "MEMORY",   icon: FloppyDisk, desc: "Memory recall" },
  { value: "trade",    label: "TRADE",    icon: Shield,    desc: "Observation only" },
  { value: "status",   label: "STATUS",   icon: Shield,    desc: "System snapshot" },
];

const ROLES = [
  { value: "",           label: "default (per-mode)" },
  { value: "strategist", label: "strategist (bull)" },
  { value: "opponent",   label: "opponent (bear)" },
  { value: "governor",   label: "governor (size)" },
  { value: "auditor",    label: "auditor (review)" },
  { value: "memory",     label: "memory (recall)" },
  { value: "executor",   label: "executor (advisory)" },
];

const SOURCE_COLOR = {
  llm_kernel:        "#22C55E",
  paradox_records:   "#F59E0B",
  static_system_data:"#06B6D4",
  safety:            "#EF4444",
};

function relTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

export default function RiseAI() {
  const [mode, setMode] = useState("chat");
  const [role, setRole] = useState("");
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [transcript, setTranscript] = useState([]); // [{role:'user'|'rise', ...}]
  const [sessionId, setSessionId] = useState(() => `console-${Math.random().toString(36).slice(2, 10)}`);
  const [currentThreadId, setCurrentThreadId] = useState(null);
  const [currentThreadTitle, setCurrentThreadTitle] = useState("");
  const [threads, setThreads] = useState([]);
  const [search, setSearch] = useState("");
  const [pinnedOnly, setPinnedOnly] = useState(false);
  const [savingThread, setSavingThread] = useState(false);
  const transcriptRef = useRef(null);

  // Auto-scroll on new message
  useEffect(() => {
    if (transcriptRef.current) {
      transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight;
    }
  }, [transcript]);

  const refreshThreads = useCallback(async () => {
    try {
      const params = { limit: 100 };
      if (pinnedOnly) params.pinned_only = true;
      if (search.trim()) params.search = search.trim();
      const r = await api.get("/admin/rise-ai/threads", { params });
      setThreads(r.data.items || []);
    } catch (e) {
      // Non-fatal — the composer still works without a refreshed thread list.
      console.debug("RiseAI refreshThreads failed", e);
    }
  }, [pinnedOnly, search]);

  useEffect(() => {
    refreshThreads();
  }, [refreshThreads]);

  const newThread = () => {
    setCurrentThreadId(null);
    setCurrentThreadTitle("");
    setSessionId(`console-${Math.random().toString(36).slice(2, 10)}`);
    setTranscript([]);
    setErr("");
  };

  const loadThread = async (tid) => {
    setErr("");
    try {
      const r = await api.post(`/admin/rise-ai/threads/${tid}/resume`);
      const data = r.data;
      setCurrentThreadId(tid);
      setCurrentThreadTitle(data.title || "");
      setSessionId(data.session_id);
      setMode(data.mode || "chat");
      setRole(data.role || "");
      setTranscript(
        (data.messages || []).map((m, i) => ({
          _id: m.id || `t-${data.session_id}-${i}`,
          kind: m.kind,
          text: m.text,
          mode: m.mode,
          role: m.role,
          call_id: m.call_id,
          provider: m.provider,
          model: m.model,
          latency_ms: m.latency_ms,
          llm_authority: m.llm_authority,
          safety_status: m.kind === "rise" ? "allowed" : undefined,
          answer_source: m.extra?.answer_source
            || (m.call_id ? "llm_kernel" : (m.kind === "rise" ? "static_system_data" : undefined)),
          extra: m.extra,
          at: m.created_at,
        })),
      );
      refreshThreads(); // bump updated_at
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  };

  const saveCurrentThread = async () => {
    if (transcript.length === 0) {
      setErr("Nothing to save — transcript is empty.");
      return;
    }
    setSavingThread(true);
    setErr("");
    try {
      if (currentThreadId) {
        // Update existing — title PATCH only here; messages already saved
        // when appended via send().
        await refreshThreads();
      } else {
        // Create new thread from the current transcript.
        const title = window.prompt(
          "Title for this thread:",
          deriveTitle(transcript),
        );
        if (!title) {
          setSavingThread(false);
          return;
        }
        const r = await api.post("/admin/rise-ai/threads", {
          title,
          session_id: sessionId,
          mode,
          role: role || null,
          messages: transcript.map((m) => ({
            kind: m.kind,
            text: m.text,
            mode: m.mode,
            role: m.role,
            call_id: m.call_id,
            provider: m.provider,
            model: m.model,
            latency_ms: m.latency_ms,
            llm_authority: m.llm_authority || "ADVISORY_ONLY",
            extra: m.extra || null,
          })),
        });
        setCurrentThreadId(r.data.thread.thread_id);
        setCurrentThreadTitle(r.data.thread.title);
        await refreshThreads();
      }
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setSavingThread(false);
    }
  };

  const togglePin = async (tid, pinned) => {
    try {
      await api.patch(`/admin/rise-ai/threads/${tid}`, { pinned: !pinned });
      refreshThreads();
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  };

  const archiveThread = async (tid) => {
    try {
      await api.patch(`/admin/rise-ai/threads/${tid}`, { archived: true });
      if (tid === currentThreadId) newThread();
      refreshThreads();
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  };

  const send = useCallback(async () => {
    const p = prompt.trim();
    if (!p) return;
    setBusy(true);
    setErr("");
    const userMsg = {
      _id: `u-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      kind: "user",
      text: p,
      mode,
      role,
      at: new Date().toISOString(),
    };
    setTranscript((t) => [...t, userMsg]);
    setPrompt("");
    try {
      const payload = { prompt: p, mode, session_id: sessionId };
      if (role) payload.role_override = role;
      const r = await api.post("/ai/run", payload);
      const data = r.data;
      const riseMsg = {
        _id: `r-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        kind: "rise",
        text: data.answer,
        mode: data.mode,
        provider: data.provider,
        model: data.model,
        latency_ms: data.latency_ms,
        call_id: data.call_id,
        safety_status: data.safety_status,
        safety_category: data.safety_category,
        llm_authority: data.llm_authority,
        answer_source: data.extra?.answer_source
          || (data.safety_status === "blocked" ? "safety"
            : data.call_id ? "llm_kernel" : "static_system_data"),
        extra: data.extra,
        at: data.created_at,
        graded: null,
      };
      setTranscript((t) => [...t, riseMsg]);

      // If we're in a saved thread, persist the two new messages.
      if (currentThreadId) {
        try {
          await api.patch(`/admin/rise-ai/threads/${currentThreadId}`, {
            append_messages: [
              {
                kind: "user", text: p, mode, role: role || null,
                llm_authority: "ADVISORY_ONLY",
              },
              {
                kind: "rise", text: data.answer, mode: data.mode,
                role: data.extra?.role || null,
                call_id: data.call_id, provider: data.provider,
                model: data.model, latency_ms: data.latency_ms,
                llm_authority: data.llm_authority,
                extra: data.extra || null,
              },
            ],
          });
          refreshThreads();
        } catch (e) {
          // Persistence is best-effort; transcript still in memory.
          console.debug("RiseAI thread persistence failed", e);
        }
      }
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
      setTranscript((t) => [
        ...t,
        {
          _id: `err-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          kind: "rise",
          text: `Error: ${e?.response?.data?.detail || e.message}`,
          at: new Date().toISOString(),
          answer_source: "safety",
        },
      ]);
    } finally {
      setBusy(false);
    }
  }, [prompt, mode, role, sessionId, currentThreadId, refreshThreads]);

  const onGrade = async (idx, score) => {
    const m = transcript[idx];
    if (!m?.call_id) return;
    try {
      await api.post(`/admin/llm/ledger/${m.call_id}/grade`, {
        score,
        outcome: score > 0 ? "helpful" : score < 0 ? "wrong" : "neutral",
      });
      setTranscript((t) => {
        const next = [...t];
        next[idx] = { ...next[idx], graded: score };
        return next;
      });
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    }
  };

  const onKey = (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !busy) send();
  };

  const pinned = threads.filter((t) => t.pinned);
  const unpinned = threads.filter((t) => !t.pinned);

  return (
    <div className="p-4 flex gap-4" data-testid="rise-ai-page">
      {/* Threads sidebar */}
      <div className="w-64 flex-shrink-0 space-y-3" data-testid="rise-ai-threads-sidebar">
        <Card>
          <div className="p-2 space-y-2">
            <button
              onClick={newThread}
              className="w-full border border-rd-text text-rd-text hover:bg-rd-text hover:text-black py-1.5 text-xs font-mono tracking-widest font-bold flex items-center justify-center gap-1"
              data-testid="rise-ai-new-thread"
            >
              <Plus size={12} weight="bold" />
              NEW THREAD
            </button>
            <div className="flex items-center gap-1 border border-rd-border px-2 py-1">
              <MagnifyingGlass size={10} className="text-rd-dim" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="search title/tag"
                className="bg-transparent border-0 outline-none text-[11px] font-mono text-rd-text placeholder:text-rd-dim flex-1 min-w-0"
                data-testid="rise-ai-search"
              />
            </div>
            <label className="flex items-center gap-1 text-[10px] font-mono text-rd-muted">
              <input
                type="checkbox"
                checked={pinnedOnly}
                onChange={(e) => setPinnedOnly(e.target.checked)}
                data-testid="rise-ai-pinned-only"
              />
              pinned only
            </label>
          </div>
        </Card>

        {pinned.length > 0 && (
          <Card>
            <div className="px-2 py-1 border-b border-rd-border text-[9px] font-mono uppercase tracking-widest text-rd-dim flex items-center gap-1">
              <PushPin size={9} weight="fill" /> pinned ({pinned.length})
            </div>
            <div className="divide-y divide-rd-border max-h-64 overflow-y-auto">
              {pinned.map((t) => (
                <ThreadItem
                  key={t.thread_id} t={t}
                  active={t.thread_id === currentThreadId}
                  onClick={() => loadThread(t.thread_id)}
                  onTogglePin={() => togglePin(t.thread_id, t.pinned)}
                  onArchive={() => archiveThread(t.thread_id)}
                />
              ))}
            </div>
          </Card>
        )}

        <Card>
          <div className="px-2 py-1 border-b border-rd-border text-[9px] font-mono uppercase tracking-widest text-rd-dim flex items-center justify-between">
            <span>saved ({unpinned.length})</span>
            <button onClick={refreshThreads} title="refresh" className="text-rd-muted hover:text-rd-text">
              <ArrowsClockwise size={10} weight="bold" />
            </button>
          </div>
          <div className="divide-y divide-rd-border max-h-96 overflow-y-auto">
            {unpinned.length === 0 && (
              <div className="px-2 py-3 text-center text-[10px] font-mono text-rd-dim">
                no saved threads
              </div>
            )}
            {unpinned.map((t) => (
              <ThreadItem
                key={t.thread_id} t={t}
                active={t.thread_id === currentThreadId}
                onClick={() => loadThread(t.thread_id)}
                onTogglePin={() => togglePin(t.thread_id, t.pinned)}
                onArchive={() => archiveThread(t.thread_id)}
              />
            ))}
          </div>
        </Card>
      </div>

      {/* Main column */}
      <div className="flex-1 min-w-0 space-y-4">
        <div className="flex items-end justify-between gap-4 flex-wrap">
          <div>
            <div className="label-eyebrow">RISE_AI · operator console</div>
            <div className="font-display text-2xl font-black tracking-tight flex items-center gap-2">
              <Brain size={22} weight="bold" />
              {currentThreadTitle || "cognition layer"}
            </div>
            <div className="text-[11px] font-mono text-rd-muted pt-1 max-w-2xl">
              {currentThreadId ? (
                <>thread <span className="text-rd-text">{currentThreadId.slice(0, 8)}</span> · every reply persists</>
              ) : (
                "Every interaction lands in the LLM Ledger and is gradable. Save the transcript as a thread to persist it."
              )}
            </div>
          </div>
          {transcript.length > 0 && !currentThreadId && (
            <button
              onClick={saveCurrentThread}
              disabled={savingThread}
              className="px-3 py-1.5 border border-[#F59E0B] text-[#F59E0B] hover:bg-[#F59E0B] hover:text-black disabled:opacity-40 flex items-center gap-1 text-xs font-mono tracking-widest font-bold"
              data-testid="rise-ai-save-thread"
            >
              <FloppyDisk size={12} weight="bold" />
              SAVE AS THREAD
            </button>
          )}
        </div>

        {/* Composer */}
        <Card>
          <div className="p-3 space-y-3">
            <div className="flex items-center gap-2 flex-wrap text-xs font-mono">
              <label className="flex items-center gap-1 text-rd-muted">
                <span className="uppercase tracking-widest text-[10px]">mode</span>
                <select
                  value={mode}
                  onChange={(e) => setMode(e.target.value)}
                  disabled={busy}
                  className="bg-black border border-rd-border text-rd-text px-1 py-0.5 focus:outline-none focus:border-rd-text"
                  data-testid="rise-ai-mode"
                >
                  {MODES.map((m) => (
                    <option key={m.value} value={m.value}>{m.label} — {m.desc}</option>
                  ))}
                </select>
              </label>
              <label className="flex items-center gap-1 text-rd-muted">
                <span className="uppercase tracking-widest text-[10px]">role</span>
                <select
                  value={role}
                  onChange={(e) => setRole(e.target.value)}
                  disabled={busy}
                  className="bg-black border border-rd-border text-rd-text px-1 py-0.5 focus:outline-none focus:border-rd-text"
                  data-testid="rise-ai-role"
                >
                  {ROLES.map((r) => (
                    <option key={r.value} value={r.value}>{r.label}</option>
                  ))}
                </select>
              </label>
              <span className="ml-auto text-rd-dim">
                session: <span className="text-rd-text">{sessionId}</span>
              </span>
            </div>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              onKeyDown={onKey}
              disabled={busy}
              rows={3}
              placeholder="Ask RISE_AI…  ( ⌘+Enter to send )"
              maxLength={8000}
              className="w-full bg-black border border-rd-border focus:border-rd-text focus:outline-none p-2 text-sm font-mono text-rd-text placeholder:text-rd-dim resize-y"
              data-testid="rise-ai-prompt"
            />
            <div className="flex items-center justify-between">
              <div className="text-[10px] font-mono text-rd-dim">
                {prompt.length}/8000
              </div>
              <button
                onClick={send}
                disabled={busy || !prompt.trim()}
                className="px-3 py-1.5 border border-rd-text text-rd-text hover:bg-rd-text hover:text-black disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1 text-xs font-mono tracking-widest font-bold"
                data-testid="rise-ai-send"
              >
                <PaperPlaneTilt size={12} weight="bold" />
                {busy ? "thinking…" : "SEND"}
              </button>
            </div>
            {err && (
              <div className="text-[11px] font-mono text-rd-danger" data-testid="rise-ai-error">
                ✕ {err}
              </div>
            )}
          </div>
        </Card>

        {/* Transcript */}
        <Card>
          <div className="px-3 py-2 border-b border-rd-border text-[10px] font-mono text-rd-dim uppercase tracking-widest flex items-center justify-between">
            <span>transcript ({transcript.length} message{transcript.length === 1 ? "" : "s"})</span>
            {transcript.length > 0 && !currentThreadId && (
              <button
                onClick={() => setTranscript([])}
                className="text-rd-muted hover:text-rd-text normal-case"
                data-testid="rise-ai-clear"
              >
                clear
              </button>
            )}
          </div>
          <div
            ref={transcriptRef}
            className="max-h-[60vh] overflow-y-auto divide-y divide-rd-border"
          >
            {transcript.length === 0 && (
              <div className="px-3 py-6 text-center text-xs font-mono text-rd-dim">
                No messages yet. Try: <span className="text-rd-text">&quot;Show system status&quot;</span> (status mode)
                · <span className="text-rd-text">&quot;Why does Doctrine (c) split the Governor and RoadGuard?&quot;</span> (reason mode)
              </div>
            )}
            {transcript.map((m, i) => (
              <Message
                key={m._id ?? `msg-${i}`}
                idx={i}
                m={m}
                onGrade={onGrade}
              />
            ))}
            {busy && (
              <div className="px-3 py-2 text-[11px] font-mono text-rd-dim">
                RISE_AI is thinking…
              </div>
            )}
          </div>
        </Card>
      </div>
    </div>
  );
}


function deriveTitle(transcript) {
  // Use the first user message, capped at 60 chars, as the default title.
  const firstUser = transcript.find((m) => m.kind === "user");
  if (!firstUser) return "Untitled thread";
  const t = (firstUser.text || "").trim().replace(/\s+/g, " ");
  return t.length > 60 ? `${t.slice(0, 57)}…` : t;
}


function ThreadItem({ t, active, onClick, onTogglePin, onArchive }) {
  return (
    <div
      className={`px-2 py-2 cursor-pointer hover:bg-white/[0.04] ${active ? "bg-white/[0.06]" : ""}`}
      onClick={onClick}
      data-testid={`rise-ai-thread-${t.thread_id}`}
    >
      <div className="flex items-start gap-1">
        <div className="flex-1 min-w-0">
          <div className="text-[11px] font-mono text-rd-text truncate font-bold" title={t.title}>
            {t.title}
          </div>
          <div className="text-[9px] font-mono text-rd-dim flex items-center gap-1 flex-wrap mt-0.5">
            <span>{t.message_count || 0} msgs</span>
            <span>·</span>
            <span>{t.mode}</span>
            {t.tags?.length > 0 && (
              <>
                <span>·</span>
                <span className="text-rd-muted truncate">{t.tags.slice(0, 3).join(", ")}</span>
              </>
            )}
          </div>
        </div>
        <div className="flex flex-col items-center gap-1">
          <button
            onClick={(e) => { e.stopPropagation(); onTogglePin(); }}
            className="text-rd-dim hover:text-rd-text"
            title={t.pinned ? "Unpin" : "Pin"}
            data-testid={`rise-ai-thread-pin-${t.thread_id}`}
          >
            <PushPin size={10} weight={t.pinned ? "fill" : "regular"} />
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); if (window.confirm(`Archive "${t.title}"?`)) onArchive(); }}
            className="text-rd-dim hover:text-rd-danger"
            title="Archive"
            data-testid={`rise-ai-thread-archive-${t.thread_id}`}
          >
            <ArchiveIcon size={10} weight="bold" />
          </button>
        </div>
      </div>
    </div>
  );
}


function Message({ idx, m, onGrade }) {
  const isUser = m.kind === "user";
  const sourceColor = SOURCE_COLOR[m.answer_source] || "#A1A1AA";

  return (
    <div
      className="px-3 py-3 space-y-1"
      data-testid={`rise-ai-msg-${idx}`}
    >
      <div className="flex items-center gap-2 text-[10px] font-mono uppercase tracking-widest">
        <span className={isUser ? "text-rd-muted" : "text-rd-text font-bold"}>
          {isUser ? "USER" : "RISE_AI"}
        </span>
        <span className="text-rd-dim">·</span>
        <span className="text-rd-dim">{m.mode}</span>
        {m.role && (
          <>
            <span className="text-rd-dim">·</span>
            <span className="text-rd-dim">role:{m.role}</span>
          </>
        )}
        {m.answer_source && !isUser && (
          <Badge color={sourceColor}>{m.answer_source.replace("_", " ")}</Badge>
        )}
        {m.safety_status === "blocked" && (
          <Badge color="#EF4444">BLOCKED · {m.safety_category}</Badge>
        )}
        <span className="ml-auto text-rd-dim normal-case">{relTime(m.at)}</span>
      </div>

      <div className="text-sm font-mono text-rd-text whitespace-pre-wrap leading-relaxed">
        {m.text}
      </div>

      {/* Trade / status mode extra blocks */}
      {m.extra?.recent_candidates && m.extra.recent_candidates.length > 0 && (
        <ExtraBlock title="recent candidates">
          {m.extra.recent_candidates.map((c) => (
            <div key={c.candidate_id} className="flex items-center gap-2">
              <span className="text-rd-text font-bold">{c.symbol}</span>
              <Badge color="#A1A1AA">{c.status}</Badge>
              <span className="text-rd-dim">{c.reason}</span>
            </div>
          ))}
        </ExtraBlock>
      )}
      {m.extra?.recent_evaluations && m.extra.recent_evaluations.length > 0 && (
        <ExtraBlock title="recent evaluations">
          {m.extra.recent_evaluations.map((e) => (
            <div key={e.evaluation_id} className="flex items-center gap-2">
              <span className="text-rd-text font-bold">{e.symbol}</span>
              <Badge color="#A1A1AA">{e.status}</Badge>
              <span className="text-rd-dim">
                {e.verdict?.final_action} · {e.verdict?.final_conviction}
              </span>
            </div>
          ))}
        </ExtraBlock>
      )}
      {m.extra?.candidates && (
        <ExtraBlock title="paradox candidates">
          <div className="grid grid-cols-4 gap-2">
            {Object.entries(m.extra.candidates).map(([k, v]) => (
              <div key={k} className="border border-rd-border px-2 py-1 text-[10px]">
                <div className="text-rd-dim uppercase tracking-widest">{k}</div>
                <div className="text-rd-text font-bold text-xs">{v}</div>
              </div>
            ))}
          </div>
        </ExtraBlock>
      )}
      {m.extra?.provider_promotion && (
        <ExtraBlock title="provider promotion">
          <div className="grid grid-cols-5 gap-2">
            {Object.entries(m.extra.provider_promotion).map(([p, s]) => (
              <div key={p} className="border border-rd-border px-2 py-1 text-[10px]">
                <div className="text-rd-dim uppercase tracking-widest">{p}</div>
                <div className="text-rd-text font-bold text-xs">{s}</div>
              </div>
            ))}
          </div>
        </ExtraBlock>
      )}

      {/* Metadata + actions (only on RISE_AI messages) */}
      {!isUser && (
        <div className="flex items-center gap-3 flex-wrap text-[10px] font-mono pt-1 border-t border-rd-border mt-2 pt-2">
          {m.provider && (
            <span className="text-rd-dim">
              provider: <span className="text-rd-text">{m.provider}</span>
            </span>
          )}
          {m.model && (
            <span className="text-rd-dim">
              model: <span className="text-rd-text">{m.model}</span>
            </span>
          )}
          {m.latency_ms != null && (
            <span className="text-rd-dim">
              latency: <span className="text-rd-text">{m.latency_ms}ms</span>
            </span>
          )}
          {m.llm_authority && (
            <Badge color="#22C55E">{m.llm_authority}</Badge>
          )}
          {m.call_id && (
            <>
              <Link
                to={`/admin/llm-ledger`}
                className="text-rd-muted hover:text-rd-text flex items-center gap-1"
                title={`Open in Ledger: ${m.call_id}`}
                data-testid={`rise-ai-open-ledger-${idx}`}
              >
                <ArrowSquareOut size={10} weight="bold" />
                open in ledger
              </Link>
              <div className="ml-auto flex items-center gap-1">
                <GradeButton
                  active={m.graded === 1}
                  color="#22C55E"
                  icon={ThumbsUp}
                  label="+1"
                  onClick={() => onGrade(idx, 1)}
                  testid={`rise-ai-grade-plus-${idx}`}
                />
                <GradeButton
                  active={m.graded === 0}
                  color="#A1A1AA"
                  icon={Minus}
                  label="0"
                  onClick={() => onGrade(idx, 0)}
                  testid={`rise-ai-grade-zero-${idx}`}
                />
                <GradeButton
                  active={m.graded === -1}
                  color="#EF4444"
                  icon={ThumbsDown}
                  label="-1"
                  onClick={() => onGrade(idx, -1)}
                  testid={`rise-ai-grade-minus-${idx}`}
                />
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}


function ExtraBlock({ title, children }) {
  return (
    <div className="mt-2 space-y-1 border border-rd-border bg-black/30 p-2">
      <div className="text-[9px] uppercase tracking-widest text-rd-dim">{title}</div>
      <div className="text-[11px] font-mono">{children}</div>
    </div>
  );
}


function GradeButton({ active, color, icon: Icon, label, onClick, testid }) {
  return (
    <button
      onClick={onClick}
      className="px-1.5 py-0.5 border flex items-center gap-0.5 tracking-widest text-[10px] font-bold"
      style={{
        borderColor: color,
        color: active ? "#000" : color,
        backgroundColor: active ? color : "transparent",
      }}
      data-testid={testid}
    >
      <Icon size={8} weight="bold" />
      {label}
    </button>
  );
}
