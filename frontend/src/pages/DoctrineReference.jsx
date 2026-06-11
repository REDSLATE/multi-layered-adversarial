import React, { useEffect, useMemo, useState } from "react";
import { PageHeader } from "@/components/ui-bits";
import { BookOpen } from "@phosphor-icons/react";
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
          No strategies registered for lane "{lane}".
        </div>
      )}

      <div data-testid="doctrine-reference-cards">
        {filtered.map((card) => (
          <StrategyCard key={card.strategy_id} card={card} />
        ))}
      </div>
    </div>
  );
}
