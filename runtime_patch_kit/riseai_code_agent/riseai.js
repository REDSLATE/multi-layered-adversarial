#!/usr/bin/env node
/**
 * RISEAI Code Agent — entry point.
 *
 * Doctrine:
 *   This tool runs ON THE BRAIN SIDE, before a sidecar opens a PR to
 *   its own repo. It's a grep tripwire + safe test runner + patch-note
 *   discipline. It is NOT a model integration and it does NOT replace
 *   MC's runtime gates — MC's tripwire pytest + doctrine-locked
 *   modules ARE the enforcement of record once an intent lands.
 *
 *   This is the cheap upstream check that catches "I accidentally
 *   touched the broker router" before the patch even merges. MC is
 *   the expensive downstream check that catches everything the grep
 *   missed.
 */
import { scanRepo } from "./agent/repoScanner.js";
import { doctrineCheck } from "./agent/doctrineGuard.js";
import { writePatch } from "./agent/patchWriter.js";
import { runTests } from "./agent/testRunner.js";
import { generateReport } from "./agent/reportWriter.js";
import { generatePrBody } from "./agent/prBody.js";

const args = process.argv.slice(2);
const cmd = args[0];

async function main() {
  if (!cmd || cmd === "help") {
    console.log(`
RISEAI Code Agent v0.4

Commands:
  scan <path>                              Walk repo, list inspectable files
  doctrine-check <file>                    Grep tripwire on a unified diff (BLOCKS on match)
  report <file> [--json]                   Structured patch review (NEVER blocks)
  pr-body <file> [--title <name>]          Generate ready-to-paste PR description
  patch-note <title> <body...>             Write a PROPOSED_ONLY patch note
  test <command...>                        Safe test runner (refuses dangerous shell)

Examples:
  riseai-code scan backend/services
  riseai-code doctrine-check patches/fix.diff
  riseai-code report patches/fix.diff
  riseai-code pr-body patches/fix.diff --title "spread snapshot fallback"
  riseai-code patch-note "spread fix" "Add snapshot spread_bps fallback"
  riseai-code test pytest tests/test_roadguard.py

doctrine-check vs report vs pr-body:
  doctrine-check  exit 0/2     gate          enforces; CI uses this
  report          exit 0       reviewer      iterates with this
  pr-body         exit 0       composer      pastes into the PR description

Doctrine surfaces this tool watches:
  broker / order routing
  RoadGuard / kill switch / PDT governor
  Executor seat / lane router
  HOLD-vs-directional promotion (load-bearing invariant)
  Council direction override
  Operator gate default-OFF discipline
  Equity/crypto lane isolation

doctrine-check WILL NOT scan operator documentation correctly unless
the input is a unified diff. Pass a real \`.diff\` / \`.patch\` file;
the tool extracts the \`+\` lines and runs patterns over those only.
`);
    return;
  }

  if (cmd === "scan") {
    await scanRepo(args[1] || ".");
    return;
  }

  if (cmd === "doctrine-check") {
    await doctrineCheck(args[1]);
    return;
  }

  if (cmd === "report") {
    const json = args.includes("--json");
    const file = args.find((a, i) => i > 0 && !a.startsWith("--"));
    await generateReport(file, { json });
    return;
  }

  if (cmd === "pr-body") {
    // Accept `--title <name>` for explicit override.
    const titleIdx = args.indexOf("--title");
    const title = titleIdx >= 0 ? args[titleIdx + 1] : null;
    const file = args.find((a, i) => i > 0 && !a.startsWith("--") && a !== title);
    await generatePrBody(file, { title });
    return;
  }

  if (cmd === "patch-note") {
    await writePatch(args[1] || "untitled", args.slice(2).join(" "));
    return;
  }

  if (cmd === "test") {
    await runTests(args.slice(1).join(" "));
    return;
  }

  throw new Error(`Unknown command: ${cmd}`);
}

main().catch((err) => {
  console.error("RISEAI ERROR:", err.message);
  process.exit(1);
});
