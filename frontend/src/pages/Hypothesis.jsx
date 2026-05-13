import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { PageHeader, Card, Badge } from "@/components/ui-bits";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { toast } from "sonner";
import {
  MagnifyingGlass, Lightning, Warning, Skull, Trash, ArrowsClockwise,
  Eye, TrendUp, CaretDown, CaretUp, ShieldWarning, Sparkle, Target,
} from "@phosphor-icons/react";

// ── client-side ticker cache ────────────────────────────────────────
// Spec: "Also a temp memory of up to 30 mins max for tickers that are
// called up more than once. Disgarded after 30mins regardless of how
// many request are made. This is a client side feature."
// In-memory only. Survives route changes (module scope) but NOT page
// refresh — that's deliberate; the cache exists to spare you LLM cost
// on repeated hamburger searches in the same session.
const CACHE_TTL_MS = 30 * 60 * 1000;
const TICKER_CACHE = new Map(); // symbol -> { result, expiresAt }

function cacheGet(symbol) {
  const row = TICKER_CACHE.get(symbol);
  if (!row) return null;
  if (Date.now() > row.expiresAt) {
    TICKER_CACHE.delete(symbol);
    return null;
  }
  return row;
}

function cachePut(symbol, result) {
  TICKER_CACHE.set(symbol, {
    result,
    expiresAt: Date.now() + CACHE_TTL_MS,
  });
}

const BRAIN_COLOR = {
  alpha: "#3B82F6",
  camaro: "#F59E0B",
  chevelle: "#10B981",
  redeye: "#DC2626",
};

const BRAIN_LABEL = {
  alpha: "ALPHA",
  camaro: "CAMARO",
  chevelle: "CHEVELLE",
  redeye: "REDEYE",
};

// ── narrative cards ─────────────────────────────────────────────────

function CollapsibleSection({ title, icon: Icon, color, children, defaultOpen = true, testid }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border-b border-rd-border last:border-b-0" data-testid={testid}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-baseline justify-between py-3 hover:bg-rd-bg/40 px-2 -mx-2"
      >
        <div className="flex items-baseline gap-2">
          {Icon && <Icon size={13} weight="bold" style={{ color }} />}
          <span className="text-[11px] font-mono uppercase tracking-[0.2em]" style={{ color }}>
            {title}
          </span>
        </div>
        {open ? <CaretUp size={12} weight="bold" /> : <CaretDown size={12} weight="bold" />}
      </button>
      {open && <div className="pb-3 px-1">{children}</div>}
    </div>
  );
}

function StrategistCard({ strategist }) {
  const meta = strategist?._meta || {};
  const err = strategist?._error;
  const parseErr = strategist?._parse_error;
  const direction = (strategist?.direction || "—").toUpperCase();
  const confidence = strategist?.confidence_pct;
  const dirColor = direction === "BUY" ? "#10B981" : direction === "SELL" ? "#DC2626" : "#A1A1AA";
  return (
    <Card
      className="border-l-2"
      accentColor="#10B981"
      testid="strategist-card"
    >
      <div className="flex items-baseline justify-between mb-3">
        <div className="flex items-baseline gap-2">
          <Sparkle size={13} weight="bold" className="text-emerald-400" />
          <span className="label-eyebrow text-emerald-400">Strategist</span>
          {meta.brain ? (
            <Badge color={BRAIN_COLOR[meta.brain] || "#A1A1AA"} testid="strategist-brain">
              {BRAIN_LABEL[meta.brain] || meta.brain}
            </Badge>
          ) : (
            <Badge color="#71717A" testid="strategist-brain">SEAT EMPTY</Badge>
          )}
        </div>
        <div className="text-[10px] font-mono text-rd-muted">
          {meta.model || "—"}
        </div>
      </div>

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 text-[11px] font-mono mb-3">
          {err}
        </div>
      )}

      {parseErr && (
        <div className="border border-amber-500 text-amber-400 px-3 py-2 text-[11px] font-mono mb-3" data-testid="strategist-parse-error">
          LLM returned non-JSON output. Raw text:
          <div className="mt-1 text-rd-text whitespace-pre-wrap">{strategist?._raw}</div>
        </div>
      )}

      {!err && !parseErr && (
        <>
          <div className="flex items-center gap-6 mb-4" data-testid="strategist-verdict">
            <div>
              <div className="text-[9px] uppercase tracking-widest text-rd-dim">Direction</div>
              <div className="text-3xl font-black tracking-tight" style={{ color: dirColor }}>
                {direction}
              </div>
            </div>
            {confidence != null && (
              <div>
                <div className="text-[9px] uppercase tracking-widest text-rd-dim">Confidence</div>
                <div className="text-3xl font-black tracking-tight text-rd-text">
                  {Math.round(confidence)}%
                </div>
              </div>
            )}
          </div>

          {strategist?.short_term_target && (
            <CollapsibleSection
              title="Short-term target (1-2w)"
              icon={Target}
              color="#86EFAC"
              testid="strategist-short-target"
            >
              <p className="text-sm text-rd-text leading-relaxed font-mono">
                {strategist.short_term_target}
              </p>
            </CollapsibleSection>
          )}

          {strategist?.medium_term_target && (
            <CollapsibleSection
              title="Medium-term target (1-3mo)"
              icon={TrendUp}
              color="#86EFAC"
              testid="strategist-medium-target"
            >
              <p className="text-sm text-rd-text leading-relaxed font-mono">
                {strategist.medium_term_target}
              </p>
            </CollapsibleSection>
          )}

          {strategist?.investment_thesis && (
            <CollapsibleSection
              title="Investment Thesis"
              icon={Sparkle}
              color="#86EFAC"
              testid="strategist-thesis"
            >
              <p className="text-sm text-rd-text leading-relaxed">
                {strategist.investment_thesis}
              </p>
            </CollapsibleSection>
          )}

          {Array.isArray(strategist?.catalysts) && strategist.catalysts.length > 0 && (
            <CollapsibleSection
              title={`Strategist Catalysts · ${strategist.catalysts.length}`}
              icon={Lightning}
              color="#86EFAC"
              testid="strategist-catalysts"
            >
              <ul className="space-y-2">
                {strategist.catalysts.map((c, i) => (
                  <li
                    key={i}
                    className="flex gap-2 text-sm text-rd-text leading-relaxed"
                    data-testid={`strategist-catalyst-${i}`}
                  >
                    <span className="text-emerald-400 font-mono pt-0.5">+</span>
                    <span>{c}</span>
                  </li>
                ))}
              </ul>
            </CollapsibleSection>
          )}
        </>
      )}
    </Card>
  );
}

function AuditorCard({ auditor }) {
  const meta = auditor?._meta || {};
  const err = auditor?._error;
  const parseErr = auditor?._parse_error;
  const verdict = (auditor?.verdict || "—").toUpperCase();
  const verdictColor =
    verdict === "ACCEPTABLE" ? "#10B981" :
    verdict === "UNACCEPTABLE" ? "#DC2626" :
    verdict === "BORDERLINE" ? "#F59E0B" : "#A1A1AA";

  return (
    <Card
      className="border-l-2"
      accentColor="#DC2626"
      testid="auditor-card"
    >
      <div className="flex items-baseline justify-between mb-3">
        <div className="flex items-baseline gap-2">
          <ShieldWarning size={13} weight="bold" className="text-rose-400" />
          <span className="label-eyebrow text-rose-400">Auditor</span>
          {meta.brain ? (
            <Badge color={BRAIN_COLOR[meta.brain] || "#A1A1AA"} testid="auditor-brain">
              {BRAIN_LABEL[meta.brain] || meta.brain}
            </Badge>
          ) : (
            <Badge color="#71717A" testid="auditor-brain">SEAT EMPTY</Badge>
          )}
        </div>
        <div className="text-[10px] font-mono text-rd-muted">
          {meta.model || "—"}
        </div>
      </div>

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 text-[11px] font-mono mb-3">
          {err}
        </div>
      )}

      {parseErr && (
        <div className="border border-amber-500 text-amber-400 px-3 py-2 text-[11px] font-mono mb-3" data-testid="auditor-parse-error">
          LLM returned non-JSON output. Raw text:
          <div className="mt-1 text-rd-text whitespace-pre-wrap">{auditor?._raw}</div>
        </div>
      )}

      {!err && !parseErr && (
        <>
          <div className="mb-4" data-testid="auditor-verdict">
            <div className="text-[9px] uppercase tracking-widest text-rd-dim">Verdict</div>
            <div className="text-3xl font-black tracking-tight" style={{ color: verdictColor }}>
              {verdict}
            </div>
          </div>

          {Array.isArray(auditor?.risk_flags) && auditor.risk_flags.length > 0 && (
            <CollapsibleSection
              title={`Auditor Risk Flags · ${auditor.risk_flags.length}`}
              icon={Warning}
              color="#FCA5A5"
              testid="auditor-risk-flags"
            >
              <ul className="space-y-2">
                {auditor.risk_flags.map((r, i) => (
                  <li
                    key={i}
                    className="flex gap-2 text-sm text-rd-text leading-relaxed"
                    data-testid={`auditor-risk-flag-${i}`}
                  >
                    <span className="text-rose-400 font-mono pt-0.5">−</span>
                    <span>{r}</span>
                  </li>
                ))}
              </ul>
            </CollapsibleSection>
          )}

          {Array.isArray(auditor?.what_could_go_wrong) && auditor.what_could_go_wrong.length > 0 && (
            <CollapsibleSection
              title="What could go wrong"
              icon={Warning}
              color="#FCA5A5"
              testid="auditor-what-could-go-wrong"
            >
              <ul className="space-y-2">
                {auditor.what_could_go_wrong.map((w, i) => (
                  <li
                    key={i}
                    className="text-sm text-rd-text leading-relaxed"
                    data-testid={`auditor-scenario-${i}`}
                  >
                    {w}
                  </li>
                ))}
              </ul>
            </CollapsibleSection>
          )}

          {Array.isArray(auditor?.kill_switch_triggers) && auditor.kill_switch_triggers.length > 0 && (
            <CollapsibleSection
              title="Kill-switch Triggers"
              icon={Skull}
              color="#FCA5A5"
              testid="auditor-kill-switch"
            >
              <ul className="space-y-2">
                {auditor.kill_switch_triggers.map((k, i) => (
                  <li
                    key={i}
                    className="flex gap-2 text-sm text-rd-text leading-relaxed font-mono"
                    data-testid={`auditor-kill-trigger-${i}`}
                  >
                    <Skull size={12} weight="bold" className="text-rose-400 mt-0.5 shrink-0" />
                    <span>{k}</span>
                  </li>
                ))}
              </ul>
            </CollapsibleSection>
          )}
        </>
      )}
    </Card>
  );
}

// ── page ─────────────────────────────────────────────────────────────

export default function Hypothesis() {
  const [symbol, setSymbol] = useState("");
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [cached, setCached] = useState(false);
  const inputRef = useRef(null);

  // Cache stats — re-rendered on every result change so the UI shows the
  // live cache size without needing a poll.
  const cacheStats = useMemo(() => {
    const now = Date.now();
    let live = 0;
    for (const [, v] of TICKER_CACHE) {
      if (v.expiresAt > now) live += 1;
    }
    return { live };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result]);

  const runAnalyze = useCallback(async (sym) => {
    const s = (sym || "").trim().toUpperCase();
    if (!s) {
      setErr("Enter a ticker symbol");
      return;
    }
    setErr("");
    // Check client-side cache first.
    const hit = cacheGet(s);
    if (hit) {
      setResult({ ...hit.result, _from_cache: true, _expires_at: hit.expiresAt });
      setCached(true);
      return;
    }
    setLoading(true);
    setCached(false);
    try {
      const res = await api.post("/hypothesis/analyze", { symbol: s });
      cachePut(s, res.data);
      setResult({ ...res.data, _from_cache: false });
    } catch (e) {
      setErr(e?.message || "Analysis failed");
      toast.error(e?.message || "Analysis failed");
    } finally {
      setLoading(false);
    }
  }, []);

  const onSubmit = (e) => {
    e.preventDefault();
    runAnalyze(symbol);
  };

  const clearAll = () => {
    setSymbol("");
    setResult(null);
    setErr("");
    setCached(false);
    inputRef.current?.focus();
  };

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  return (
    <div className="space-y-6" data-testid="hypothesis-page">
      <PageHeader
        eyebrow="Adversarial AI"
        title="AI Investment Hypothesis"
        sub="Strategist generates thesis · Auditor stress-tests it. Two LLMs, two persona seats, one truth."
        right={
          <div className="flex items-baseline gap-3 text-[10px] font-mono uppercase tracking-widest text-rd-dim">
            <span>cache · <span className="text-rd-text">{cacheStats.live}</span> tickers · ttl 30m</span>
          </div>
        }
        testid="hypothesis-header"
      />

      <Card testid="hypothesis-search-card">
        <form onSubmit={onSubmit} className="flex items-stretch gap-2">
          <div className="relative flex-1">
            <MagnifyingGlass size={14} weight="bold" className="absolute left-3 top-1/2 -translate-y-1/2 text-rd-dim" />
            <Input
              ref={inputRef}
              value={symbol}
              onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              placeholder="Ticker (e.g. NVDA, AAPL, TSLA)"
              data-testid="hypothesis-symbol-input"
              className="pl-9 h-11 font-mono text-base tracking-wide bg-rd-bg border-rd-border uppercase"
              autoComplete="off"
              maxLength={10}
              disabled={loading}
            />
          </div>
          <Button
            type="submit"
            disabled={loading || !symbol.trim()}
            data-testid="hypothesis-analyze-btn"
            className="h-11 px-6 bg-emerald-500 hover:bg-emerald-400 text-black font-mono uppercase tracking-wider text-xs"
          >
            {loading ? (
              <>
                <ArrowsClockwise size={14} weight="bold" className="animate-spin mr-2" />
                Analyzing
              </>
            ) : (
              <>
                <Lightning size={14} weight="bold" className="mr-2" />
                Analyze
              </>
            )}
          </Button>
          {(result || symbol) && (
            <Button
              type="button"
              variant="outline"
              onClick={clearAll}
              disabled={loading}
              data-testid="hypothesis-clear-btn"
              className="h-11 px-4 font-mono uppercase tracking-wider text-xs"
            >
              <Trash size={12} weight="bold" className="mr-1.5" />
              Clear
            </Button>
          )}
        </form>
        {err && (
          <div className="mt-3 border border-rd-danger text-rd-danger px-3 py-2 text-[11px] font-mono" data-testid="hypothesis-error">
            {err}
          </div>
        )}
        {result && (
          <div className="mt-3 flex items-baseline gap-3 text-[10px] font-mono uppercase tracking-widest text-rd-dim">
            <Eye size={11} weight="bold" />
            <span>analysis · {result.symbol}</span>
            <span>·</span>
            <span>generated {new Date(result.generated_at).toLocaleTimeString()}</span>
            {cached && (
              <>
                <span>·</span>
                <span className="text-amber-400" data-testid="hypothesis-cached-tag">CACHED</span>
                <span className="text-rd-muted">
                  · expires in {Math.max(0, Math.round((result._expires_at - Date.now()) / 60000))}m
                </span>
              </>
            )}
          </div>
        )}
      </Card>

      {result && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4" data-testid="hypothesis-result">
          <StrategistCard strategist={result.strategist} />
          <AuditorCard auditor={result.auditor} />
        </div>
      )}

      {result && (
        <Card testid="hypothesis-context-card">
          <div className="label-eyebrow mb-2">Anchored Context</div>
          <div className="text-[11px] font-mono text-rd-muted leading-relaxed">
            {result.context?.has_market_context ? (
              <>
                Latest snapshot:{" "}
                <span className="text-rd-text">
                  {result.context.indicator_snapshot?.symbol} · {result.context.indicator_snapshot?.tf} ·{" "}
                  {result.context.indicator_snapshot?.source}
                </span>
                {result.context.indicator_snapshot?.indicators?.last_close != null && (
                  <> · last close ${result.context.indicator_snapshot.indicators.last_close.toFixed(2)}</>
                )}
              </>
            ) : (
              <span className="text-amber-400">
                No live indicator snapshot for {result.symbol} — narratives drawn from general priors only.
              </span>
            )}
            <div className="mt-1">
              · open positions: {result.context?.open_positions?.length || 0}
              · recent intents: {result.context?.recent_intents?.length || 0}
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
