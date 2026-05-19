/**
 * PR-body generator — emits a ready-to-paste pull-request description
 * containing everything an operator needs to approve a patch without
 * reverse-engineering it from the diff.
 *
 * Composes the existing pieces:
 *   - report (risk, surfaces, warnings, recommended tests)
 *   - the patch's diffstat (files + line counts)
 *   - operator checklists: rollout AND rollback
 *
 * Doctrine pin: this command synthesizes review material; it does not
 * approve, merge, or deploy. Exit 0 always. The output is markdown
 * the brain agent pastes into the PR description.
 *
 * Why a rollback checklist:
 *   "How do I undo this?" is the question operators ask AFTER a bad
 *   merge, when context is gone and the brain agent isn't online.
 *   Forcing that answer into the PR body at WRITE time means the
 *   rollback exists BEFORE it's ever needed.
 */
import fs from "fs";
import {
  DANGEROUS_PATHS,
  FORBIDDEN_PATTERNS,
  extractAddedLines,
} from "./doctrineGuard.js";
import {
  scoreRisk,
  buildRecommendedTests,
  operatorSummary,
  detectSurfacesAndWarnings,
} from "./reportWriter.js";


// Parse the `+++ b/path/to/file` headers to list touched files.
// Excludes `+++` markers without a real path (rare malformed diffs).
function extractTouchedFiles(text) {
  const files = [];
  const lines = text.split(/\r?\n/);
  for (const line of lines) {
    if (!line.startsWith("+++ ")) continue;
    let path = line.slice(4).trim();
    if (path.startsWith("b/")) path = path.slice(2);
    if (path && path !== "/dev/null") files.push(path);
  }
  return files;
}


// Count + and - lines (excluding headers) to produce a diffstat
// without needing `git diffstat` on the host.
function diffStat(text) {
  let added = 0;
  let removed = 0;
  const lines = text.split(/\r?\n/);
  for (const line of lines) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) added++;
    else if (line.startsWith("-")) removed++;
  }
  return { added, removed };
}


function rolloutChecklist(risk, touchedFiles) {
  const items = [
    "[ ] Patch reviewed against the doctrine surfaces listed above",
    "[ ] Recommended tests run locally and pass",
    "[ ] Brain agent confirms no MC runtime gate is being bypassed",
  ];
  if (risk === "MEDIUM" || risk === "HIGH") {
    items.push("[ ] Operator has read the doctrine warnings");
    items.push("[ ] Patch note exists and is committed");
  }
  if (risk === "HIGH") {
    items.push("[ ] Tripwire suite (`pytest -m tripwire`) run and passing");
    items.push("[ ] Two-operator review on this PR before merge");
  }
  if (touchedFiles.some((f) => f.endsWith(".env") || f.includes("config"))) {
    items.push("[ ] No production secrets in the diff");
    items.push("[ ] Environment delta documented in the patch note");
  }
  return items;
}


function rollbackChecklist(risk, touchedFiles) {
  // The rollback checklist exists to answer the question operators
  // ask AFTER a bad merge: "how do I undo this without making it
  // worse?" The base steps are universal; risk-tier adds more.
  const items = [
    "[ ] `git revert <merge-sha>` is the safe default — does NOT rewrite history",
    "[ ] Brain redeploys from pre-merge SHA if revert needs to ship fast",
  ];
  if (risk === "MEDIUM" || risk === "HIGH") {
    items.push(
      "[ ] Confirm MC's diagnostic endpoints still return 200 after rollback " +
        "(snapshot-completeness, sidecar-checkin, snapshot-contract)",
    );
    items.push("[ ] Re-run brain-local tests against the reverted code");
  }
  if (risk === "HIGH") {
    items.push(
      "[ ] If the patch touched broker_router / executor_seat / kill_switch: " +
        "verify MC's `RISEDUAL_BROKER_REQUIRE_MC_RECEIPT` state didn't drift " +
        "during the window the patch was live",
    );
    items.push(
      "[ ] If any positions were opened during the patch's live window: " +
        "audit the receipts against MC's `execution_receipts` collection",
    );
    items.push("[ ] Tripwire suite passes against the reverted code");
  }
  if (touchedFiles.some((f) => f.includes("mongo") || f.includes("db.py") || f.includes("namespaces"))) {
    items.push(
      "[ ] Mongo-shape change rolled back? If the patch added a collection " +
        "or index, the rollback does NOT remove it — drop manually if needed",
    );
  }
  return items;
}


export async function generatePrBody(patchFile, opts = {}) {
  if (!patchFile) {
    throw new Error("Missing patch file. Usage: riseai-code pr-body <file>");
  }
  if (!fs.existsSync(patchFile)) {
    throw new Error(`File does not exist: ${patchFile}`);
  }

  const text = fs.readFileSync(patchFile, "utf8");
  const added = extractAddedLines(text);
  const stat = diffStat(text);
  const touchedFiles = extractTouchedFiles(text);

  let scanText;
  let scanMode;
  let addedCount;
  if (added === null) {
    scanText = text;
    scanMode = "WHOLE-FILE";
    addedCount = stat.added;
  } else {
    scanText = added.join("\n");
    scanMode = "DIFF";
    addedCount = added.length;
  }

  const { touched, warnings } = detectSurfacesAndWarnings(scanText);
  const risk = scoreRisk(touched, warnings);
  const recommendedTests = buildRecommendedTests(touched, risk);
  const summary = operatorSummary(risk, touched, warnings, addedCount);
  const rollout = rolloutChecklist(risk, touchedFiles);
  const rollback = rollbackChecklist(risk, touchedFiles);

  // The patch title — accept --title flag or default to the file's
  // base name. The brain agent should override on the CLI when the
  // patch's purpose isn't obvious from the filename.
  const title = opts.title || patchFile.split("/").pop().replace(/\.(diff|patch)$/i, "");

  const md = renderMarkdown({
    title,
    risk,
    summary,
    scanMode,
    addedCount,
    stat,
    touchedFiles,
    touched,
    warnings,
    recommendedTests,
    rollout,
    rollback,
  });

  console.log(md);
  return md;
}


function renderMarkdown(d) {
  const lines = [];
  lines.push(`# ${d.title}`);
  lines.push("");
  lines.push(`**Risk:** \`${d.risk}\` · **Mode:** \`${d.scanMode}\` · ` +
    `**Diff:** +${d.stat.added} −${d.stat.removed} across ${d.touchedFiles.length} file${d.touchedFiles.length === 1 ? "" : "s"}`);
  lines.push("");
  lines.push(`> ${d.summary}`);
  lines.push("");

  lines.push("## Files touched");
  if (d.touchedFiles.length === 0) {
    lines.push("_(no `+++` headers found — pass a real unified diff)_");
  } else {
    for (const f of d.touchedFiles) lines.push(`- \`${f}\``);
  }
  lines.push("");

  lines.push("## Doctrine review");
  lines.push("");
  lines.push("**Touched surfaces:**");
  if (d.touched.length === 0) {
    lines.push("- _(none)_");
  } else {
    for (const s of d.touched) lines.push(`- \`${s}\``);
  }
  lines.push("");
  lines.push("**Doctrine warnings:**");
  if (d.warnings.length === 0) {
    lines.push("- _(none — clean against grep tripwire)_");
  } else {
    for (const w of d.warnings) {
      lines.push(`- **${w.name}** — ${w.message}`);
    }
  }
  lines.push("");

  lines.push("## Recommended tests");
  for (const t of d.recommendedTests) lines.push(`- \`${t}\``);
  lines.push("");
  lines.push("> These are suggestions, not authority. MC's tripwire suite remains the source of runtime truth.");
  lines.push("");

  lines.push("## Rollout checklist");
  for (const item of d.rollout) lines.push(`- ${item}`);
  lines.push("");

  lines.push("## Rollback checklist");
  for (const item of d.rollback) lines.push(`- ${item}`);
  lines.push("");

  lines.push("## Intent");
  lines.push("_(brain agent: replace this with the patch's intent — what user-visible behavior changes, why now, what alternatives were considered)_");
  lines.push("");

  lines.push("---");
  lines.push("_Generated by `riseai-code pr-body`. This body is review material, " +
    "not approval. MC runtime gates remain the source of truth._");

  return lines.join("\n");
}


// Exported for unit tests / programmatic use.
export { extractTouchedFiles, diffStat, rolloutChecklist, rollbackChecklist };
