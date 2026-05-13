import React, { useEffect, useRef, useState } from "react";
import { useTier } from "../context/TierContext";
import { mc } from "../lib/mc";
import { Send, Sparkles, Lock, RotateCcw } from "lucide-react";

function ProMaxGate() {
  return (
    <div
      data-testid="rd-chat-promax-gate"
      className="rounded-xl border border-slate-700 bg-gradient-to-br from-slate-800 to-slate-900 p-10 text-center"
    >
      <div className="mx-auto mb-4 inline-flex h-10 w-10 items-center justify-center rounded-md bg-amber-500/10 text-amber-400">
        <Lock size={18} strokeWidth={1.8} />
      </div>
      <div className="font-display text-xl text-white">RiseDualGPT is Pro Max only.</div>
      <p className="mx-auto mt-3 max-w-md text-[13px] text-zinc-400">
        Grounded multi-turn chat with the council's live data. Switch the
        tier selector to <span className="font-mono text-emerald-300">Pro Max</span> in the
        header to start a conversation.
      </p>
    </div>
  );
}

function Bubble({ role, text }) {
  const isUser = role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        data-testid={`rd-chat-bubble-${role}`}
        className={
          "max-w-[78%] rounded-lg px-4 py-3 text-[14px] leading-relaxed " +
          (isUser
            ? "bg-emerald-500/15 text-emerald-50 border border-emerald-500/20"
            : "bg-slate-800/60 text-zinc-100 border border-slate-700")
        }
      >
        {!isUser && (
          <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-[0.18em] text-emerald-300">
            <img src="/risedual/mark_chat.png" alt="" className="h-3.5 w-auto" />
            RiseDualGPT
          </div>
        )}
        <div className="whitespace-pre-wrap">{text}</div>
      </div>
    </div>
  );
}

export default function Chat() {
  const { tier } = useTier();
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState(null);
  const [sessionId, setSessionId] = useState(null);
  const scrollRef = useRef(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, sending]);

  if (tier !== "pro_max") {
    return (
      <div className="space-y-8" data-testid="rd-chat-page">
        <div>
          <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-zinc-500">
            RiseDualGPT
          </div>
          <h1 className="mt-2 font-display text-3xl tracking-tight text-white md:text-4xl">
            Ask the council.
          </h1>
        </div>
        <ProMaxGate />
      </div>
    );
  }

  const send = async () => {
    const msg = input.trim();
    if (!msg || sending) return;
    setError(null);
    setSending(true);
    setMessages((m) => [...m, { role: "user", text: msg }]);
    setInput("");
    const res = await mc.chat(tier, msg, sessionId || undefined);
    setSending(false);
    if (!res.ok) {
      setError(res.detail);
      return;
    }
    if (res.data?.session_id) setSessionId(res.data.session_id);
    setMessages((m) => [...m, { role: "assistant", text: res.data?.reply || "" }]);
  };

  const reset = () => {
    setMessages([]);
    setSessionId(null);
    setError(null);
  };

  return (
    <div className="space-y-6" data-testid="rd-chat-page">
      <div className="flex items-end justify-between">
        <div>
          <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-zinc-500">
            RiseDualGPT · Claude Sonnet 4.5
          </div>
          <h1 className="mt-2 font-display text-3xl tracking-tight text-white md:text-4xl">
            Ask the council.
          </h1>
        </div>
        {messages.length > 0 && (
          <button
            onClick={reset}
            data-testid="rd-chat-reset"
            className="inline-flex items-center gap-2 rounded-md border border-slate-600 bg-slate-800/60 px-3 py-1.5 text-[12px] text-zinc-400 transition-colors hover:border-slate-500 hover:text-white"
          >
            <RotateCcw size={12} strokeWidth={2} /> New session
          </button>
        )}
      </div>

      <div className="rounded-xl border border-slate-700 bg-slate-900">
        <div
          ref={scrollRef}
          data-testid="rd-chat-transcript"
          className="h-[480px] space-y-4 overflow-y-auto p-6"
        >
          {messages.length === 0 && (
            <div className="flex h-full flex-col items-center justify-center text-center">
              <img
                src="/risedual/logo_chat.png"
                alt="RiseDualGPT"
                className="mb-6 h-44 w-auto drop-shadow-[0_0_32px_rgba(16,185,129,0.18)]"
              />
              <div className="font-display text-base text-white">Grounded in MC's live data.</div>
              <p className="mt-2 max-w-md text-[13px] text-zinc-400">
                Ask about a ticker, today's signals, the consensus on a position,
                or what the AIs disagree about right now.
              </p>
            </div>
          )}
          {messages.map((m, i) => (
            <Bubble key={i} role={m.role} text={m.text} />
          ))}
          {sending && (
            <div data-testid="rd-chat-typing" className="flex justify-start">
              <div className="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-3">
                <div className="flex items-center gap-1">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400 [animation-delay:120ms]" />
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400 [animation-delay:240ms]" />
                </div>
              </div>
            </div>
          )}
        </div>

        {error && (
          <div
            data-testid="rd-chat-error"
            className="border-t border-rose-900/40 bg-rose-950/20 px-4 py-2 text-[12px] text-rose-300"
          >
            {error}
          </div>
        )}

        <div className="flex items-center gap-2 border-t border-slate-700 p-3">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            disabled={sending}
            placeholder="Ask about a ticker, signal, or what the council sees…"
            data-testid="rd-chat-input"
            className="flex-1 bg-transparent px-3 py-2 text-[14px] text-zinc-100 placeholder-zinc-600 focus:outline-none"
          />
          <button
            onClick={send}
            disabled={sending || !input.trim()}
            data-testid="rd-chat-send"
            className="inline-flex items-center gap-2 rounded-md bg-emerald-500 px-4 py-2 text-[13px] font-medium text-black transition-colors hover:bg-emerald-400 disabled:cursor-not-allowed disabled:bg-zinc-800 disabled:text-zinc-600"
          >
            Send <Send size={13} strokeWidth={2} />
          </button>
        </div>
      </div>
    </div>
  );
}
