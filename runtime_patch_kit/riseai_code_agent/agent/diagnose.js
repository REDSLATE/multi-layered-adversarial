/**
 * diagnose — LLM-aware patch proposal command (v0.6).
 *
 * Doctrine:
 *   `diagnose` reads a curated slice of the repo, asks an LLM to
 *   propose a patch for the operator's question, and writes the
 *   proposal to disk for human review. It NEVER applies the patch.
 *   The output is review material, not a commit.
 *
 *   The CLI demands the operator hand-pick context paths (`--paths`)
 *   rather than auto-walking the repo. Two reasons:
 *     1. Context-window honesty — the operator KNOWS what got sent.
 *     2. Cost-honesty — you pay per token; you pick what burns.
 *
 *   The output is two files per run:
 *     <out>/<ts>-<slug>.md       — analysis + proposed patch + tests
 *     <out>/<ts>-<slug>.patch    — extracted unified diff (if any)
 *
 *   The proposal markdown is meant to be pasted into a PR or read
 *   alongside the existing `report` / `pr-body` outputs.
 */
import fs from "fs";
import path from "path";

import { callLLM, defaultModel, PROVIDERS, resolveApiKey } from "./llmProvider.js";


const DEFAULT_OUT_DIR = "./riseai-proposals";
const DEFAULT_MAX_FILE_BYTES = 50_000;
const DEFAULT_PROVIDER = "anthropic";


const SYSTEM_PROMPT = `You are RISEAI Code Agent, a doctrine-aware patch proposer for the RISEDUAL multi-brain trading platform.

Your job is to read the provided repo files and the operator's question, then produce a SINGLE proposal in this strict format:

## Analysis
Concise diagnosis of the root cause. 3-6 bullet points max.

## Proposed Patch
A unified diff (\`---\` / \`+++\` headers, hunk markers, \`+\`/\`-\` lines) that applies cleanly against the provided files. If no code change is needed, write the literal word "NONE" instead of a diff.

## Tests
A list of pytest -k invocations or curl commands that VERIFY the patch worked. One per line.

## Rollback
The one-command rollback if the patch causes a regression (e.g. \`git revert <sha>\` or specific code reversal steps).

## Risk
One of LOW / MEDIUM / HIGH and one sentence why.

Doctrine you MUST respect:
- MC is a notary; it does not judge trade quality. Do NOT add confidence/quality vetoes to MC code paths.
- Governor sizes (dampeners), RoadGuard caps (spread/volume), Opponent vetoes, Executor (Camaro) routes. Don't conflate.
- Memory provenance is strict: only VE (Verified Execution) memory is trainable.
- Role anchors are fixed: alpha=strategist, camaro=executor, chevelle=governor, redeye=opponent, shelly=memory. Do not propose role swaps.
- Never propose a patch that bypasses /api/execution/submit for live trades.
- Never propose default-ON execution gates.
- If a tripwire test would break, mention it explicitly in Analysis.

Output ONLY the five-section proposal. No preamble, no closing remarks.`;


// ─────────────────────────────── CLI entry ────────────────────────────


export async function runDiagnose(args) {
  const parsed = parseArgs(args);

  if (!parsed.question) {
    console.error("ERROR: diagnose requires a question as the first positional argument.");
    console.error('Example: riseai-code diagnose "spread guard rejects when bps_field missing" --paths backend/shared/execution.py');
    process.exit(1);
  }
  if (!PROVIDERS.includes(parsed.provider)) {
    console.error(`ERROR: unknown provider '${parsed.provider}'. Expected one of: ${PROVIDERS.join(", ")}`);
    process.exit(1);
  }
  if (parsed.paths.length === 0) {
    console.error("ERROR: --paths is required. Pass one or more file paths (comma-separated) for context.");
    console.error('Example: --paths backend/shared/execution.py,backend/shared/roadguard.py');
    process.exit(1);
  }

  // Resolve API key UP FRONT so we fail fast before reading files.
  const keyInfo = resolveApiKey(parsed.provider);
  if (!keyInfo.value) {
    console.error(`ERROR: no API key for ${parsed.provider}; set ${keyInfo.envName} in your environment.`);
    process.exit(1);
  }

  const model = parsed.model || defaultModel(parsed.provider);

  // Read context files.
  const contextBlocks = [];
  let totalBytes = 0;
  let included = 0;
  let skipped = 0;
  for (const p of parsed.paths) {
    const result = readFileSafely(p, parsed.maxBytes);
    if (!result.ok) {
      console.warn(`SKIP ${p}: ${result.reason}`);
      skipped += 1;
      continue;
    }
    contextBlocks.push(formatContextBlock(p, result.content, result.truncated));
    totalBytes += result.bytesUsed;
    included += 1;
  }

  if (included === 0) {
    console.error("ERROR: no readable files in --paths. Aborting before LLM call.");
    process.exit(1);
  }

  // Compose user prompt.
  const userPrompt = [
    `OPERATOR QUESTION:`,
    parsed.question,
    "",
    `REPO FILES (${included} file${included === 1 ? "" : "s"}, ${totalBytes} bytes total):`,
    "",
    contextBlocks.join("\n\n"),
  ].join("\n");

  // Print pre-flight summary so the operator sees what's about to burn.
  console.log("RISEAI DIAGNOSE");
  console.log(`provider=${parsed.provider} · model=${model}`);
  console.log(`question=${truncate(parsed.question, 100)}`);
  console.log(`context_files=${included} (${skipped} skipped) · context_bytes=${totalBytes}`);
  console.log("");
  console.log("calling provider…");

  // Make the call.
  const t0 = Date.now();
  let result;
  try {
    result = await callLLM({
      provider: parsed.provider,
      model,
      system: SYSTEM_PROMPT,
      user: userPrompt,
      maxTokens: parsed.maxTokens,
    });
  } catch (e) {
    console.error(`LLM call failed: ${e.message}`);
    process.exit(2);
  }
  const elapsedMs = Date.now() - t0;

  // Persist proposal + extracted patch.
  ensureDir(parsed.outDir);
  const slug = makeSlug(parsed.question);
  const ts = makeTimestamp();
  const mdPath = path.join(parsed.outDir, `${ts}-${slug}.md`);
  const patchPath = path.join(parsed.outDir, `${ts}-${slug}.patch`);

  const proposal = result.text || "";
  const header = composeHeader({
    question: parsed.question,
    provider: parsed.provider,
    model,
    contextFiles: parsed.paths,
    includedCount: included,
    skippedCount: skipped,
    elapsedMs,
    usage: result.usage,
  });
  fs.writeFileSync(mdPath, `${header}\n\n${proposal}\n`);

  const extractedPatch = extractDiff(proposal);
  if (extractedPatch) {
    fs.writeFileSync(patchPath, extractedPatch);
  }

  // Print summary so the operator can scrape it.
  console.log("");
  console.log(`elapsed=${elapsedMs}ms${result.usage ? ` · usage=${JSON.stringify(result.usage)}` : ""}`);
  console.log(`proposal_md=${mdPath}`);
  if (extractedPatch) {
    console.log(`extracted_patch=${patchPath}`);
  } else {
    console.log("extracted_patch=<none — model proposed NONE or no unified-diff was detected>");
  }
  console.log("");
  console.log("REMINDER: this is a PROPOSAL. Review it, then run `riseai-code doctrine-check` on the extracted patch before applying.");
}


// ─────────────────────────────── arg parsing ──────────────────────────


function parseArgs(args) {
  // args is the slice AFTER the "diagnose" command keyword.
  const out = {
    question: null,
    provider: DEFAULT_PROVIDER,
    model: null,
    paths: [],
    outDir: DEFAULT_OUT_DIR,
    maxBytes: DEFAULT_MAX_FILE_BYTES,
    maxTokens: 4096,
  };
  let i = 0;
  while (i < args.length) {
    const a = args[i];
    if (a === "--provider") {
      out.provider = args[++i];
    } else if (a === "--model") {
      out.model = args[++i];
    } else if (a === "--paths") {
      const raw = args[++i] || "";
      out.paths = raw.split(",").map((s) => s.trim()).filter(Boolean);
    } else if (a === "--out") {
      out.outDir = args[++i];
    } else if (a === "--max-bytes") {
      out.maxBytes = parseInt(args[++i], 10) || DEFAULT_MAX_FILE_BYTES;
    } else if (a === "--max-tokens") {
      out.maxTokens = parseInt(args[++i], 10) || 4096;
    } else if (a.startsWith("--")) {
      console.warn(`WARN: unknown flag ${a} ignored`);
    } else if (out.question === null) {
      out.question = a;
    } else {
      // Allow multi-word questions if user didn't quote: glue them in.
      out.question = `${out.question} ${a}`;
    }
    i += 1;
  }
  return out;
}


// ─────────────────────────────── file helpers ─────────────────────────


function readFileSafely(p, maxBytes) {
  try {
    const stat = fs.statSync(p);
    if (!stat.isFile()) {
      return { ok: false, reason: "not a regular file" };
    }
    const buf = fs.readFileSync(p);
    const truncated = buf.length > maxBytes;
    const slice = truncated ? buf.slice(0, maxBytes) : buf;
    return {
      ok: true,
      content: slice.toString("utf8"),
      bytesUsed: slice.length,
      truncated,
    };
  } catch (e) {
    return { ok: false, reason: e.message };
  }
}


function formatContextBlock(p, content, truncated) {
  const marker = truncated ? " [TRUNCATED]" : "";
  return [
    "════════════════════════════════════════════════════════════════════",
    `FILE: ${p}${marker}`,
    "════════════════════════════════════════════════════════════════════",
    content,
  ].join("\n");
}


function ensureDir(dir) {
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
}


// ─────────────────────────────── output helpers ───────────────────────


function makeTimestamp() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}` +
    `-${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}`
  );
}


function makeSlug(s) {
  return (s || "untitled")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40) || "untitled";
}


function truncate(s, n) {
  if (!s) return "";
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}


function composeHeader({ question, provider, model, contextFiles, includedCount, skippedCount, elapsedMs, usage }) {
  const lines = [
    `# RISEAI Diagnose Proposal`,
    "",
    `**Question**: ${question}`,
    `**Provider**: ${provider}`,
    `**Model**: ${model}`,
    `**Context files**: ${includedCount} included, ${skippedCount} skipped`,
    `**Generated at**: ${new Date().toISOString()}`,
    `**Latency**: ${elapsedMs}ms`,
  ];
  if (usage) {
    lines.push(`**Token usage**: \`${JSON.stringify(usage)}\``);
  }
  lines.push("");
  lines.push(`**Context paths**:`);
  for (const p of contextFiles) {
    lines.push(`- \`${p}\``);
  }
  lines.push("");
  lines.push("---");
  return lines.join("\n");
}


/**
 * Best-effort extraction of a unified-diff block from the LLM output.
 * Looks for the first line starting with `--- ` followed by a line
 * starting with `+++ ` and extracts contiguous diff content from
 * there. Strips trailing markdown fencing if present.
 *
 * Public so tests can poke it without invoking the whole flow.
 */
export function extractDiff(text) {
  if (!text) return null;
  // Strip code-fence wrappers (```diff ... ``` or ``` ... ```).
  const stripped = text.replace(/```(?:diff|patch)?\n([\s\S]*?)\n```/g, "$1");
  const lines = stripped.split(/\r?\n/);
  let start = -1;
  for (let i = 0; i < lines.length - 1; i += 1) {
    if (lines[i].startsWith("--- ") && lines[i + 1].startsWith("+++ ")) {
      start = i;
      break;
    }
  }
  if (start < 0) return null;
  // Walk forward until we hit a line that looks like markdown header
  // for a new section (`## ` at column 0) or two blank lines.
  let end = lines.length;
  let blankRun = 0;
  for (let i = start; i < lines.length; i += 1) {
    const ln = lines[i];
    if (ln.startsWith("## ")) { end = i; break; }
    if (ln.trim() === "") {
      blankRun += 1;
      if (blankRun >= 2) { end = i; break; }
    } else {
      blankRun = 0;
    }
  }
  const block = lines.slice(start, end).join("\n").trim();
  return block.length > 0 ? `${block}\n` : null;
}
