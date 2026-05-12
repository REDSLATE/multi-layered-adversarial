/**
 * Mission Control public-API client — drop-in.
 *
 * IMPORTANT: this client must run on YOUR backend (Node/Next API
 * route / serverless function), NEVER in the browser. It carries the
 * shared trust token; if exposed client-side, anyone can call MC
 * directly bypassing your auth and credit gating.
 *
 * Recommended pattern: thin proxy routes on risedual.ai's backend that
 * call this client and forward responses verbatim.
 *
 *   import { mcPublic } from "@/lib/mcPublicClient";
 *
 *   // in your /api/dashboard route:
 *   const signals = await mcPublic(user.tier).signals();
 *   res.json(signals);
 *
 * Configuration (env vars on YOUR backend):
 *   MC_BASE_URL       — e.g. "https://mc.risedual.io"
 *   MC_PUBLIC_TOKEN   — same value as MC's RISEDUAL_PUBLIC_TOKEN env var
 */
import type {
  AgentActivityResponse,
  ChatHistoryResponse,
  ChatRequest,
  ChatResponse,
  DigestResponse,
  HeatmapResponse,
  ModelsMindResponse,
  NarrativeResponse,
  ScannerPresetId,
  ScannerPresetsResponse,
  ScannerScanResponse,
  SectorsResponse,
  SignalDetail,
  SignalsResponse,
  Tier,
} from "./types";

class MCError extends Error {
  constructor(
    public readonly status: number,
    public readonly endpoint: string,
    public readonly body: string,
  ) {
    super(`MC ${endpoint} → ${status}: ${body.slice(0, 240)}`);
    this.name = "MCError";
  }
}

function requireEnv(key: string): string {
  const v = process.env[key];
  if (!v) throw new Error(`missing required env var ${key}`);
  return v;
}

async function call<T>(
  path: string,
  tier: Tier,
  init: RequestInit = {},
): Promise<T> {
  const base = requireEnv("MC_BASE_URL").replace(/\/+$/, "");
  const token = requireEnv("MC_PUBLIC_TOKEN");
  const url = `${base}${path}`;
  const res = await fetch(url, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-RiseDual-Token": token,
      "X-RiseDual-User-Tier": tier,
      ...(init.headers || {}),
    },
  });
  const body = await res.text();
  if (!res.ok) throw new MCError(res.status, path, body);
  return JSON.parse(body) as T;
}

/**
 * Create a tier-bound MC client. Call this per request with the
 * caller's tier propagated from your user model.
 */
export function mcPublic(tier: Tier) {
  return {
    signals: (limit = 20): Promise<SignalsResponse> =>
      call(`/api/public/signals?limit=${limit}`, tier),

    signal: (id: string): Promise<SignalDetail> =>
      call(`/api/public/signals/${encodeURIComponent(id)}`, tier),

    digest: (): Promise<DigestResponse> => call(`/api/public/digest`, tier),

    digestNarrative: (): Promise<NarrativeResponse> =>
      call(`/api/public/digest/narrative`, tier),

    scannerPresets: (): Promise<ScannerPresetsResponse> =>
      call(`/api/public/scanner/presets`, tier),

    scan: (presetId: ScannerPresetId): Promise<ScannerScanResponse> =>
      call(
        `/api/public/scanner/scan?preset_id=${encodeURIComponent(presetId)}`,
        tier,
      ),

    agentActivity: (opts: { since?: string; limit?: number } = {}): Promise<AgentActivityResponse> => {
      const q = new URLSearchParams();
      if (opts.since) q.set("since", opts.since);
      if (opts.limit != null) q.set("limit", String(opts.limit));
      return call(`/api/public/agent-activity/feed?${q.toString()}`, tier);
    },

    modelsMind: (symbol: string): Promise<ModelsMindResponse> =>
      call(`/api/public/models-mind/${encodeURIComponent(symbol)}`, tier),

    heatmap: (): Promise<HeatmapResponse> => call(`/api/public/heatmap`, tier),

    sectors: (): Promise<SectorsResponse> => call(`/api/public/sectors`, tier),

    // ── Pro Max only ────────────────────────────────────────────
    // Caller MUST verify the user's tier is "pro_max" before invoking
    // chat or chat-history. MC returns 403 otherwise, but you should
    // also deduct credits BEFORE calling MC per your existing pricing
    // (chat=1 credit per turn).

    chat: (body: ChatRequest): Promise<ChatResponse> =>
      call(`/api/public/chat`, tier, {
        method: "POST",
        body: JSON.stringify(body),
      }),

    chatHistory: (sessionId: string): Promise<ChatHistoryResponse> =>
      call(`/api/public/chat/history/${encodeURIComponent(sessionId)}`, tier),

    chatClear: (sessionId: string): Promise<{ deleted: number; session_id: string }> =>
      call(`/api/public/chat/history/${encodeURIComponent(sessionId)}`, tier, {
        method: "DELETE",
      }),
  };
}

export { MCError };
export type { Tier };
