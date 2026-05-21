/**
 * diagnose smoke tests — no network. Validates:
 *   1. extractDiff handles fenced + unfenced unified diffs
 *   2. extractDiff returns null when no diff present
 *   3. llmProvider rejects unknown provider names
 *   4. resolveApiKey reports missing env keys correctly
 *   5. defaultModel returns a non-empty string per provider
 *
 * Run with: node agent/test_diagnose.js
 * Exits 0 on success, 1 on failure.
 */
import { extractDiff } from "./diagnose.js";
import { PROVIDERS, callLLM, defaultModel, resolveApiKey } from "./llmProvider.js";

let passed = 0;
let failed = 0;

function ok(name, cond, detail = "") {
  if (cond) {
    passed += 1;
    console.log(`[PASS] ${name}${detail ? ` — ${detail}` : ""}`);
  } else {
    failed += 1;
    console.log(`[FAIL] ${name}${detail ? ` — ${detail}` : ""}`);
  }
}

async function fails(fn) {
  try {
    await fn();
    return null;
  } catch (e) {
    return e.message;
  }
}

// ─── extractDiff ──────────────────────────────────────────────────────

const fencedDiff = "## Proposed Patch\n```diff\n--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,3 @@\n def x():\n+    return 1\n     pass\n```\n\n## Tests\npytest";
const unfencedDiff = "## Proposed Patch\n--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,3 @@\n def x():\n+    return 1\n     pass\n\n## Tests\npytest";
const noDiff = "## Proposed Patch\nNONE\n\n## Tests\npytest";

const d1 = extractDiff(fencedDiff);
ok("extractDiff: fenced ```diff block", d1 && d1.includes("--- a/foo.py") && d1.includes("+    return 1"), d1 ? `${d1.split("\n").length} lines` : "null");

const d2 = extractDiff(unfencedDiff);
ok("extractDiff: unfenced diff stops at next ##", d2 && d2.includes("--- a/foo.py") && !d2.includes("## Tests"), d2 ? `${d2.split("\n").length} lines` : "null");

const d3 = extractDiff(noDiff);
ok("extractDiff: returns null when no diff present", d3 === null, `got=${d3}`);

const d4 = extractDiff("");
ok("extractDiff: empty input → null", d4 === null);


// ─── llmProvider invariants ───────────────────────────────────────────

ok("PROVIDERS includes anthropic/openai/gemini",
   PROVIDERS.includes("anthropic") && PROVIDERS.includes("openai") && PROVIDERS.includes("gemini"),
   `got=${PROVIDERS.join(",")}`);

for (const p of PROVIDERS) {
  const m = defaultModel(p);
  ok(`defaultModel(${p}) is non-empty`, typeof m === "string" && m.length > 0, m);
  const info = resolveApiKey(p);
  ok(`resolveApiKey(${p}) returns env-name`, typeof info.envName === "string" && info.envName.endsWith("_API_KEY"), info.envName);
}

const unknownErr = await fails(() => callLLM({ provider: "foobar", system: "x", user: "y" }));
ok("callLLM rejects unknown provider", unknownErr && unknownErr.includes("unsupported provider"), unknownErr);

// Save env, blank the keys, ensure callLLM fails-fast with the env-name guidance.
const saved = { a: process.env.ANTHROPIC_API_KEY, o: process.env.OPENAI_API_KEY, g: process.env.GEMINI_API_KEY };
delete process.env.ANTHROPIC_API_KEY;
delete process.env.OPENAI_API_KEY;
delete process.env.GEMINI_API_KEY;
const missingErr = await fails(() => callLLM({ provider: "anthropic", system: "x", user: "y" }));
ok("callLLM fails-fast when key missing", missingErr && missingErr.includes("ANTHROPIC_API_KEY"), missingErr);
// Restore
if (saved.a) process.env.ANTHROPIC_API_KEY = saved.a;
if (saved.o) process.env.OPENAI_API_KEY = saved.o;
if (saved.g) process.env.GEMINI_API_KEY = saved.g;

console.log("");
console.log(`SUMMARY: ${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
