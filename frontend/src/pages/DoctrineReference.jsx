import React, { useEffect, useMemo, useState } from "react";
import { PageHeader } from "@/components/ui-bits";
import { BookOpen, Download, CheckCircle } from "@phosphor-icons/react";
import { useAuth } from "@/context/AuthContext";

const LANES = ["all", "equity", "universal"];

function laneColor(lane) {
  if (lane === "equity") return "#3B82F6";
  if (lane === "universal") return "#F59E0B";
  return "var(--rd-dim)";
}

function FieldList({ items, mono = false }) {
  if (!items || items.length === 0) {
    return (
      <div className="text-[10px] font-mono text-rd-dim italic">
        (none — function reads from upstream modules only)
      </div>
    );
  }
  return (
    <ul className="space-y-1">
      {items.map((it, idx) => (
        <li
          key={idx}
          className={`text-[11px] ${mono ? "font-mono" : ""} leading-relaxed text-rd-text pl-3 relative before:content-['—'] before:absolute before:left-0 before:text-rd-dim`}
        >
          {it}
        </li>
      ))}
    </ul>
  );
}

function SectionTitle({ children }) {
  return (
    <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim mb-1.5">
      {children}
    </div>
  );
}

function StrategyCard({ card }) {
  const color = laneColor(card.lane);
  return (
    <div
      data-testid={`doctrine-card-${card.strategy_id}`}
      className="border bg-rd-bg2 p-4 mb-4"
      style={{ borderColor: "var(--rd-border)", borderLeft: `3px solid ${color}` }}
    >
      <div className="flex items-baseline justify-between mb-1 flex-wrap gap-2">
        <div className="text-base font-semibold text-rd-text">{card.title}</div>
        <div className="flex items-center gap-2">
          <span
            className="text-[9px] font-mono uppercase tracking-widest px-1.5 py-0.5 border"
            style={{ borderColor: color, color }}
          >
            {card.lane}
          </span>
          <span className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">
            {card.category}
          </span>
        </div>
      </div>

      <div className="text-[12px] italic text-rd-dim mb-3">{card.tagline}</div>

      <div className="text-[10px] font-mono text-rd-dim mb-4 flex flex-wrap gap-x-3 gap-y-1">
        <span>
          <span className="text-rd-text">version:</span> {card.doctrine_version}
        </span>
        <span>
          <span className="text-rd-text">source:</span> {card.source_attribution}
        </span>
        <span>
          <span className="text-rd-text">module:</span> {card.source_module}
        </span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-4">
        <div>
          <SectionTitle>Ideal Conditions</SectionTitle>
          <FieldList items={card.ideal_conditions} />
        </div>
        <div>
          <SectionTitle>Entries</SectionTitle>
          <FieldList items={card.entries} />
        </div>
        <div>
          <SectionTitle>Exits</SectionTitle>
          <FieldList items={card.exits} />
        </div>
        <div>
          <SectionTitle>Size Modifiers</SectionTitle>
          <FieldList items={card.size_modifier_notes} />
        </div>
        <div>
          <SectionTitle>Snapshot Fields Read</SectionTitle>
          <FieldList items={card.snapshot_fields_read} mono />
        </div>
        <div>
          <SectionTitle>Risk Flags / Labels Read</SectionTitle>
          <FieldList items={card.risk_flags_read} mono />
        </div>
      </div>
    </div>
  );
}

function LanePill({ value, current, onClick }) {
  const active = value === current;
  return (
    <button
      data-testid={`doctrine-ref-lane-${value}`}
      onClick={() => onClick(value)}
      className="px-3 py-1 text-[11px] font-mono uppercase tracking-wider border transition-colors"
      style={{
        borderColor: active ? "var(--rd-text)" : "var(--rd-border)",
        color: active ? "var(--rd-text)" : "var(--rd-dim)",
        background: active ? "var(--rd-bg2)" : "transparent",
      }}
    >
      {value}
    </button>
  );
}

function TrainingExportBlock({ token, strategyIds }) {
  const apiBase = `${process.env.REACT_APP_BACKEND_URL}/api/admin/doctrine-training`;
  const [busy, setBusy] = useState(false);

  const download = async () => {
    setBusy(true);
    try {
      const resp = await fetch(`${apiBase}/jsonl`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `risedual_doctrine_corpus_${new Date().toISOString().slice(0, 10)}.jsonl`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      // eslint-disable-next-line no-alert
      alert(`Export failed: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="mb-4 p-3 border border-rd-border bg-rd-bg2 flex items-center justify-between flex-wrap gap-3"
      data-testid="doctrine-training-block"
    >
      <div>
        <div className="text-[10px] font-mono uppercase tracking-widest text-rd-dim">
          Fine-tune Corpus
        </div>
        <div className="text-[12px] text-rd-text mt-0.5">
          JSONL training pairs built from {strategyIds.length} live cards
          (q&amp;a, rules, fields, code, comparisons). Same anti-drift contract
          — code is the only source.
        </div>
      </div>
      <button
        onClick={download}
        disabled={busy}
        data-testid="doctrine-training-download"
        className="flex items-center gap-2 px-3 py-1.5 text-[11px] font-mono uppercase tracking-wider border border-rd-text text-rd-text hover:bg-rd-text hover:text-rd-bg transition-colors disabled:opacity-50"
      >
        <Download size={14} />
        {busy ? "Building…" : "Download JSONL"}
      </button>
    </div>
  );
}

function EvalBlock({ token }) {
  const apiBase = `${process.env.REACT_APP_BACKEND_URL}/api/admin/doctrine-eval`;
  const [questions, setQuestions] = useState([]);
  const [selectedId, setSelectedId] = useState("");
  const [response, setResponse] = useState("");
  const [scoreResult, setScoreResult] = useState(null);
  const [scoring, setScoring] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`${apiBase}/questions`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        const j = await r.json();
        if (!cancelled) {
          setQuestions(j.questions || []);
          if (j.questions?.length && !selectedId) {
            setSelectedId(j.questions[0].id);
          }
        }
      } catch {
        /* noop */
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const selected = questions.find((q) => q.id === selectedId);

  const runScore = async () => {
    if (!selected) return;
    setScoring(true);
    setScoreResult(null);
    try {
      const r = await fetch(`${apiBase}/score`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ eval_id: selected.id, response }),
      });
      const j = await r.json();
      setScoreResult(j);
    } catch (e) {
      setScoreResult({ error: String(e.message || e) });
    } finally {
      setScoring(false);
    }
  };

  if (questions.length === 0) return null;

  return (
    <div
      className="mb-6 p-4 border border-rd-border bg-rd-bg2"
      data-testid="doctrine-eval-block"
    >
      <div className="text-[10px] font-mono uppercase tracking-widest text-rd-dim mb-1">
        Doctrine Eval
      </div>
      <div className="text-[11px] text-rd-dim mb-3">
        Keyword-overlap scoring of an LLM response against expected card fields.
        Questions are auto-generated from cards — they cannot go stale.
      </div>
      <div className="grid grid-cols-1 md:grid-cols-[1fr_2fr] gap-3 items-start">
        <select
          value={selectedId}
          onChange={(e) => {
            setSelectedId(e.target.value);
            setScoreResult(null);
          }}
          data-testid="doctrine-eval-question-select"
          className="bg-rd-bg border border-rd-border text-rd-text text-[11px] font-mono p-2"
        >
          {questions.map((q) => (
            <option key={q.id} value={q.id}>
              {q.id}
            </option>
          ))}
        </select>
        <div>
          {selected && (
            <div className="text-[12px] text-rd-text mb-2" data-testid="doctrine-eval-question-text">
              {selected.q}
            </div>
          )}
          <textarea
            value={response}
            onChange={(e) => setResponse(e.target.value)}
            placeholder="Paste a brain / LLM response here…"
            data-testid="doctrine-eval-response"
            className="w-full h-20 bg-rd-bg border border-rd-border text-rd-text text-[11px] font-mono p-2 resize-y"
          />
          <div className="flex items-center gap-3 mt-2">
            <button
              onClick={runScore}
              disabled={scoring || !response.trim()}
              data-testid="doctrine-eval-score-btn"
              className="flex items-center gap-2 px-3 py-1 text-[11px] font-mono uppercase tracking-wider border border-rd-text text-rd-text hover:bg-rd-text hover:text-rd-bg transition-colors disabled:opacity-50"
            >
              <CheckCircle size={14} />
              {scoring ? "Scoring…" : "Score"}
            </button>
            {scoreResult && !scoreResult.error && (
              <div
                className="text-[11px] font-mono text-rd-text"
                data-testid="doctrine-eval-score-result"
              >
                Score:{" "}
                <span style={{ color: scoreResult.score >= 0.5 ? "#22c55e" : "#f59e0b" }}>
                  {(scoreResult.score * 100).toFixed(0)}%
                </span>{" "}
                <span className="text-rd-dim">
                  ({scoreResult.matched_keywords.length}/{
                    scoreResult.matched_keywords.length + scoreResult.missed_keywords.length
                  } keywords)
                </span>
              </div>
            )}
            {scoreResult?.error && (
              <div className="text-[11px] text-red-400">{scoreResult.error}</div>
            )}
          </div>
          {scoreResult && !scoreResult.error && scoreResult.missed_keywords?.length > 0 && (
            <div className="text-[10px] font-mono text-rd-dim mt-2">
              Missed: {scoreResult.missed_keywords.join(", ")}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function DoctrineReference() {
  const { token } = useAuth();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [lane, setLane] = useState("all");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      try {
        const url = `${process.env.REACT_APP_BACKEND_URL}/api/admin/doctrine-reference`;
        const resp = await fetch(url, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        const j = await resp.json();
        if (!cancelled) {
          setData(j);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(String(e.message || e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const filtered = useMemo(() => {
    if (!data?.strategies) return [];
    if (lane === "all") return data.strategies;
    return data.strategies.filter((s) => s.lane === lane);
  }, [data, lane]);

  return (
    <div data-testid="doctrine-reference-page">
      <PageHeader
        icon={BookOpen}
        title="Doctrine Reference"
        subtitle={
          "Live operator cards generated directly from doctrine code. "
          + "Anti-drift: a CI test fails if any card's snapshot fields or risk "
          + "flags do not appear in the function source. No static documentation, "
          + "no hallucinations."
        }
        testid="doctrine-reference-header"
      />

      <div className="mb-4 flex items-center gap-2 flex-wrap">
        <span className="text-[10px] font-mono uppercase tracking-widest text-rd-dim">
          Lane
        </span>
        {LANES.map((l) => (
          <LanePill key={l} value={l} current={lane} onClick={setLane} />
        ))}
        {data && (
          <span
            className="text-[10px] font-mono text-rd-dim ml-auto"
            data-testid="doctrine-reference-meta"
          >
            {filtered.length} of {data.count} strategies · generated{" "}
            {data.generated_at?.slice(11, 19)}Z
          </span>
        )}
      </div>

      {loading && (
        <div
          className="text-[11px] font-mono text-rd-dim p-4 border border-rd-border bg-rd-bg2"
          data-testid="doctrine-reference-loading"
        >
          Loading live doctrine…
        </div>
      )}

      {error && (
        <div
          className="text-[11px] font-mono p-4 border border-red-700 bg-red-950/40 text-red-300"
          data-testid="doctrine-reference-error"
        >
          Failed to load doctrine reference: {error}
        </div>
      )}

      {!loading && !error && filtered.length === 0 && (
        <div
          className="text-[11px] font-mono text-rd-dim p-4 border border-rd-border bg-rd-bg2"
          data-testid="doctrine-reference-empty"
        >
          No strategies registered for lane &quot;{lane}&quot;.
        </div>
      )}

      {!loading && !error && data && (
        <>
          <TrainingExportBlock
            token={token}
            strategyIds={(data.strategies || []).map((s) => s.strategy_id)}
          />
          <EvalBlock token={token} />
        </>
      )}

      <div data-testid="doctrine-reference-cards">
        {filtered.map((card) => (
          <StrategyCard key={card.strategy_id} card={card} />
        ))}
      </div>
    </div>
  );
}
