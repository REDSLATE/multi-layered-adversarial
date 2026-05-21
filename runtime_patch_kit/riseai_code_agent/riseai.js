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
 *
 * Why lazy imports:
 *   `self-check` is the deterministic install-health signal. If we
 *   eagerly imported every agent module at the top of this file, a
 *   missing/broken module would crash before `self-check` could
 *   report which module is the problem. Lazy per-command imports
 *   mean `riseai-code self-check` works even when other modules are
 *   broken — exactly the failure mode the brain agent needs to see.
 */

const args = process.argv.slice(2);
const cmd = args[0];

async function main() {
  if (!cmd || cmd === "help") {
    console.log(`
RISEAI Code Agent v0.6

Commands:
  scan <path>                              Walk repo, list inspectable files
  doctrine-check <file>                    Grep tripwire on a unified diff (BLOCKS on match)
  report <file> [--json]                   Structured patch review (NEVER blocks)
  pr-body <file> [--title <name>]          Generate ready-to-paste PR description
  patch-note <title> <body...>             Write a PROPOSED_ONLY patch note
  test <command...>                        Safe test runner (refuses dangerous shell)
  diagnose <question> [opts]               LLM patch proposer (writes review material to disk)
  self-check                               Run installation health checks (exit 0=healthy, 1=broken)
  version                                  Print kit version + runtime context

Examples:
  riseai-code self-check
  riseai-code version
  riseai-code scan backend/services
  riseai-code doctrine-check patches/fix.diff
  riseai-code report patches/fix.diff
  riseai-code pr-body patches/fix.diff --title "spread snapshot fallback"
  riseai-code patch-note "spread fix" "Add snapshot spread_bps fallback"
  riseai-code test pytest tests/test_roadguard.py
  riseai-code diagnose "spread guard rejects when bps missing" \\
      --paths backend/shared/execution.py,backend/shared/roadguard.py \\
      --provider anthropic --model claude-sonnet-4-5-20250929

diagnose flags:
  --provider <anthropic|openai|gemini>  (default: anthropic)
  --model <model-id>                    (default: provider's recommended)
  --paths <p1,p2,...>                   (REQUIRED — files to include as context)
  --out <dir>                           (default: ./riseai-proposals)
  --max-bytes <n>                       (per-file size cap, default: 50000)
  --max-tokens <n>                      (LLM output ceiling, default: 4096)

API keys (required for diagnose):
  ANTHROPIC_API_KEY  /  OPENAI_API_KEY  /  GEMINI_API_KEY

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

  // Lazy imports — each command imports only the module it needs so
  // a broken/missing module in one path doesn't take down `version` or
  // `self-check`.

  if (cmd === "self-check") {
    const { runSelfCheck } = await import("./agent/selfCheck.js");
    const exitCode = await runSelfCheck();
    process.exit(exitCode);
  }

  if (cmd === "version" || cmd === "--version" || cmd === "-v") {
    const { printVersion } = await import("./agent/selfCheck.js");
    await printVersion();
    return;
  }

  if (cmd === "scan") {
    const { scanRepo } = await import("./agent/repoScanner.js");
    await scanRepo(args[1] || ".");
    return;
  }

  if (cmd === "doctrine-check") {
    const { doctrineCheck } = await import("./agent/doctrineGuard.js");
    await doctrineCheck(args[1]);
    return;
  }

  if (cmd === "report") {
    const { generateReport } = await import("./agent/reportWriter.js");
    const json = args.includes("--json");
    const file = args.find((a, i) => i > 0 && !a.startsWith("--"));
    await generateReport(file, { json });
    return;
  }

  if (cmd === "pr-body") {
    const { generatePrBody } = await import("./agent/prBody.js");
    const titleIdx = args.indexOf("--title");
    const title = titleIdx >= 0 ? args[titleIdx + 1] : null;
    const file = args.find((a, i) => i > 0 && !a.startsWith("--") && a !== title);
    await generatePrBody(file, { title });
    return;
  }

  if (cmd === "patch-note") {
    const { writePatch } = await import("./agent/patchWriter.js");
    await writePatch(args[1] || "untitled", args.slice(2).join(" "));
    return;
  }

  if (cmd === "test") {
    const { runTests } = await import("./agent/testRunner.js");
    await runTests(args.slice(1).join(" "));
    return;
  }

  if (cmd === "diagnose") {
    const { runDiagnose } = await import("./agent/diagnose.js");
    await runDiagnose(args.slice(1));
    return;
  }

  throw new Error(`Unknown command: ${cmd}`);
}

main().catch((err) => {
  console.error("RISEAI ERROR:", err.message);
  process.exit(1);
});
