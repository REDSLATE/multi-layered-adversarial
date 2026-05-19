/**
 * Doctrine guard — grep tripwire on a unified diff.
 *
 * THE FIX (v0.2):
 *   v0.1 scanned the entire file content, which false-positived on
 *   operator documentation that legitimately discussed HOLD / BUY /
 *   broker / RoadGuard in prose. v0.2 parses the unified diff first
 *   and runs patterns ONLY against ADDED LINES (lines starting with
 *   `+` but NOT `+++` headers). Removed lines, unchanged context,
 *   and the diff metadata itself are ignored.
 *
 *   If the input file is NOT a unified diff (no `---`/`+++` headers),
 *   the tool emits a LOUD warning and falls back to whole-file scan
 *   so an operator can still grep a raw `.py` if they want — but the
 *   intended usage is `git diff > patch.diff && riseai-code doctrine-check patch.diff`.
 *
 * Doctrine surfaces this tool watches (all listed verbatim so the
 * `.diff` reviewer can know what triggered without reading code):
 */
import fs from "fs";

const DANGEROUS_PATHS = [
  "broker_router",
  "broker_adapter",
  "live_position",
  "live_positions",
  "roadguard",
  "pdt",
  "kill_switch",
  "exposure_cap",
  "executor_seat",
  "auto_router",
  "execution_authority",
];

const FORBIDDEN_PATTERNS = [
  {
    name: "HOLD promotion",
    pattern: /HOLD[\s\S]{0,80}(BUY|SELL|SHORT|COVER)|(?:BUY|SELL|SHORT|COVER)[\s\S]{0,80}HOLD/i,
    message:
      "Added code appears to mix HOLD with directional actions in a single expression. " +
      "HOLD is never directional — see DIRECTIONAL_ACTIONS invariant.",
  },
  {
    name: "Council direction override",
    pattern: /council[\s\S]{0,120}(direction|override|BUY|SELL|SHORT|COVER)/i,
    message:
      "Added code appears to let the council MODIFY trade direction. " +
      "Council may modulate SIZE only, never direction.",
  },
  {
    name: "RoadGuard bypass",
    pattern:
      /skip[_ ]*roadguard|bypass[_ ]*roadguard|ROADGUARD_CAN_APPROVE\s*=\s*(true|True|1)/i,
    message:
      "Added code appears to disable or invert RoadGuard. " +
      "RoadGuard rejects; it never approves.",
  },
  {
    name: "Operator gate default ON",
    pattern:
      /(OPERATOR_GATE|LIVE_TRADING|REQUIRE_RECEIPT|EXECUTION_ENABLED)\s*[:=]\s*(true|True|1)\b/i,
    message:
      "Added code appears to default an execution/operator gate to ON. " +
      "All such gates default OFF and require explicit operator opt-in.",
  },
  {
    name: "may_override re-introduction",
    pattern: /\bmay_override\b/i,
    message:
      "Added code references `may_override`. This field was removed " +
      "from doctrine on 2026-02-19 (4-seat merge). Do not re-introduce.",
  },
  {
    name: "Decider/advisor re-introduction",
    pattern:
      /(["'])(?:decider|crypto_decider|advisor|crypto_advisor)\1/i,
    message:
      "Added code references the deprecated seat names " +
      "(decider/advisor). Use canonical 4-seat names: executor, " +
      "governor, opponent, auditor.",
  },
];


/**
 * Parse a unified diff and return ONLY the added lines (without the
 * leading `+`). Headers (`+++`) are excluded. Returns an empty array
 * if the input doesn't look like a unified diff.
 */
function extractAddedLines(text) {
  const lines = text.split(/\r?\n/);
  const looksLikeDiff =
    lines.some((l) => l.startsWith("--- ")) &&
    lines.some((l) => l.startsWith("+++ "));

  if (!looksLikeDiff) return null;

  const added = [];
  for (const line of lines) {
    // `+++` is the file header — never an added line.
    if (line.startsWith("+++")) continue;
    if (line.startsWith("+")) {
      added.push(line.slice(1));
    }
  }
  return added;
}


export async function doctrineCheck(patchFile) {
  if (!patchFile) {
    throw new Error("Missing patch file. Usage: riseai-code doctrine-check <file>");
  }
  if (!fs.existsSync(patchFile)) {
    throw new Error(`File does not exist: ${patchFile}`);
  }

  const text = fs.readFileSync(patchFile, "utf8");
  const added = extractAddedLines(text);

  let scanText;
  let scanMode;
  if (added === null) {
    // Not a diff — fall back to whole-file but flag it loudly so the
    // operator knows the result includes prose context, not just
    // intended edits.
    scanText = text;
    scanMode = "WHOLE-FILE (no diff headers found — false positives likely)";
  } else if (added.length === 0) {
    console.log("RISEAI DOCTRINE CHECK: PASS");
    console.log("  scan mode: DIFF (0 added lines)");
    return;
  } else {
    scanText = added.join("\n");
    scanMode = `DIFF (${added.length} added line${added.length === 1 ? "" : "s"})`;
  }

  const lowerScan = scanText.toLowerCase();
  const warnings = [];

  for (const p of DANGEROUS_PATHS) {
    if (lowerScan.includes(p)) {
      warnings.push(`Dangerous surface touched: ${p}`);
    }
  }

  for (const rule of FORBIDDEN_PATTERNS) {
    if (rule.pattern.test(scanText)) {
      warnings.push(`${rule.name}: ${rule.message}`);
    }
  }

  if (warnings.length) {
    console.log("RISEAI DOCTRINE CHECK: BLOCKED");
    console.log(`  scan mode: ${scanMode}`);
    for (const w of warnings) console.log("-", w);
    console.log(
      "\nNote: this is a grep tripwire, not MC's runtime enforcement. " +
        "False positives are possible. If the patch is legitimate, document " +
        "the override in the patch note and pass the operator review.",
    );
    process.exit(2);
  }

  console.log("RISEAI DOCTRINE CHECK: PASS");
  console.log(`  scan mode: ${scanMode}`);
}


// Exported for unit testing.
export { extractAddedLines, DANGEROUS_PATHS, FORBIDDEN_PATTERNS };
