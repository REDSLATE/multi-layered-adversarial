/**
 * Report generator — produces a structured review of a unified diff.
 *
 * Same grep machinery as `doctrineGuard.js`, but instead of blocking
 * on the first match it summarizes EVERY signal and emits a YAML-ish
 * report. Pipes cleanly to a PR description or a patch note.
 *
 *   risk_level:        LOW | MEDIUM | HIGH
 *   touched_surfaces:  [list]
 *   doctrine_warnings: [list]
 *   recommended_tests: [list]
 *   operator_summary:  one-line plain-English read
 *
 * Doctrine pin: this command does NOT block. Exit 0 even on HIGH
 * risk. The output is review material, not an authority. The
 * operator decides what to do with it.
 */
import fs from "fs";
import {
  DANGEROUS_PATHS,
  FORBIDDEN_PATTERNS,
  extractAddedLines,
} from "./doctrineGuard.js";


// Surfaces that map to "look at MC's tripwire tests before merging."
// Touching any of these elevates risk floor to HIGH even if no
// pattern fires — the tripwires exist BECAUSE these modules are
// load-bearing and grep alone can't prove a change is safe.
const HIGH_RISK_SURFACES = new Set([
  "broker_router",
  "broker_adapter",
  "kill_switch",
  "roadguard",
  "executor_seat",
  "auto_router",
  "execution_authority",
  "pdt",
]);


// Map a touched surface to the pytest target the brain agent (or
// operator) should run before merging. These are SUGGESTIONS, not
// authoritative — the real source of truth is MC's tripwire suite
// (`pytest -m tripwire`). If a surface isn't mapped here, the
// fallback recommendation is to run the full tripwire suite.
const SURFACE_TO_TEST = {
  broker_router: "pytest -k broker_router -v",
  broker_adapter: "pytest -k broker -v",
  kill_switch: "pytest -k kill_switch -v",
  roadguard: "pytest -k roadguard -v",
  executor_seat: "pytest -k 'seat or executor' -v",
  auto_router: "pytest -k auto_router -v",
  execution_authority: "pytest -k 'authority or execution' -v",
  pdt: "pytest -k pdt -v",
  live_position: "pytest -k position -v",
  live_positions: "pytest -k position -v",
  exposure_cap: "pytest -k exposure -v",
};


/**
 * Score the risk level from the matched signals.
 *
 *   HIGH   — any forbidden pattern fires, OR any high-risk surface touched
 *   MEDIUM — only a non-high-risk dangerous surface touched (e.g. live_position)
 *   LOW    — no surfaces, no patterns
 */
function scoreRisk(surfaces, warnings) {
  if (warnings.length > 0) return "HIGH";
  for (const s of surfaces) {
    if (HIGH_RISK_SURFACES.has(s)) return "HIGH";
  }
  if (surfaces.length > 0) return "MEDIUM";
  return "LOW";
}


function buildRecommendedTests(surfaces, risk) {
  const targeted = new Set();
  for (const s of surfaces) {
    if (SURFACE_TO_TEST[s]) targeted.add(SURFACE_TO_TEST[s]);
  }
  const out = Array.from(targeted);
  // Always recommend the tripwire backstop on HIGH. On LOW/MEDIUM,
  // recommend it only if no targeted test was identified — otherwise
  // we'd be telling the agent to run the whole suite for a tiny patch.
  if (risk === "HIGH" || out.length === 0) {
    out.push("pytest -m tripwire");
  }
  return out;
}


function operatorSummary(risk, surfaces, warnings, addedCount) {
  if (risk === "LOW") {
    return `${addedCount} added line${addedCount === 1 ? "" : "s"}, no sensitive surfaces touched.`;
  }
  if (risk === "MEDIUM") {
    return (
      `${addedCount} added line${addedCount === 1 ? "" : "s"}, ` +
      `touches ${surfaces.length} sensitive surface${surfaces.length === 1 ? "" : "s"} ` +
      `(${surfaces.join(", ")}) without firing any forbidden pattern. Review recommended.`
    );
  }
  // HIGH
  const parts = [];
  if (warnings.length) parts.push(`${warnings.length} doctrine warning${warnings.length === 1 ? "" : "s"}`);
  if (surfaces.length) parts.push(`touches ${surfaces.join(", ")}`);
  return `${addedCount} added lines; ${parts.join(" and ")}. Operator approval required before merge.`;
}


function detectSurfacesAndWarnings(scanText) {
  const lowerScan = scanText.toLowerCase();

  const touched = [];
  for (const p of DANGEROUS_PATHS) {
    if (lowerScan.includes(p)) touched.push(p);
  }

  const warnings = [];
  for (const rule of FORBIDDEN_PATTERNS) {
    if (rule.pattern.test(scanText)) {
      warnings.push({ name: rule.name, message: rule.message });
    }
  }
  return { touched, warnings };
}


function renderYaml(report) {
  const lines = [];
  lines.push(`risk_level: ${report.risk_level}`);
  lines.push(`scan_mode: ${report.scan_mode}`);
  lines.push(`added_lines: ${report.added_lines}`);

  lines.push("touched_surfaces:");
  if (report.touched_surfaces.length === 0) {
    lines.push("  (none)");
  } else {
    for (const s of report.touched_surfaces) lines.push(`  - ${s}`);
  }

  lines.push("doctrine_warnings:");
  if (report.doctrine_warnings.length === 0) {
    lines.push("  (none)");
  } else {
    for (const w of report.doctrine_warnings) {
      lines.push(`  - name: ${w.name}`);
      lines.push(`    message: ${w.message}`);
    }
  }

  lines.push("recommended_tests:");
  for (const t of report.recommended_tests) lines.push(`  - ${t}`);

  lines.push(`operator_summary: ${report.operator_summary}`);
  return lines.join("\n");
}


export async function generateReport(patchFile, opts = {}) {
  if (!patchFile) {
    throw new Error("Missing patch file. Usage: riseai-code report <file>");
  }
  if (!fs.existsSync(patchFile)) {
    throw new Error(`File does not exist: ${patchFile}`);
  }

  const text = fs.readFileSync(patchFile, "utf8");
  const added = extractAddedLines(text);

  let scanText;
  let scanMode;
  let addedCount;
  if (added === null) {
    scanText = text;
    scanMode = "WHOLE-FILE";
    addedCount = text.split(/\r?\n/).length;
  } else {
    scanText = added.join("\n");
    scanMode = "DIFF";
    addedCount = added.length;
  }

  const { touched, warnings } = detectSurfacesAndWarnings(scanText);
  const risk = scoreRisk(touched, warnings);
  const recommended = buildRecommendedTests(touched, risk);
  const summary = operatorSummary(risk, touched, warnings, addedCount);

  const report = {
    risk_level: risk,
    scan_mode: scanMode,
    added_lines: addedCount,
    touched_surfaces: touched,
    doctrine_warnings: warnings,
    recommended_tests: recommended,
    operator_summary: summary,
  };

  if (opts.json) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    console.log(renderYaml(report));
  }

  // Doctrine pin: REPORT does not block. Exit 0 regardless of risk.
  // The operator chooses what to do with the report.
  return report;
}


// Exported for unit tests / programmatic use.
export { scoreRisk, buildRecommendedTests, operatorSummary, detectSurfacesAndWarnings };
