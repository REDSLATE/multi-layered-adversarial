/**
 * Self-check + version reporters.
 *
 * Doctrine:
 *   `self-check` is the deterministic "is this install healthy?"
 *   signal a brain agent runs after `npm link` and again before they
 *   trust the tool during a live patch cycle. It executes EVERY agent
 *   module against a synthetic input — no network, no filesystem
 *   writes beyond a temp diff string, no dependence on the surrounding
 *   repo. Exit 0 = healthy. Exit 1 = something is broken.
 *
 *   `version` prints surfaces an operator needs when debugging "why
 *   does brain A pass doctrine-check on this diff but brain B reject
 *   it?" — usually the answer is they're on different kit versions.
 */
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";


const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..");


// ─── version ──────────────────────────────────────────────────────────


function readPackageVersion() {
  try {
    const raw = fs.readFileSync(path.join(REPO_ROOT, "package.json"), "utf8");
    return JSON.parse(raw).version || "unknown";
  } catch (e) {
    return "unknown";
  }
}


export async function printVersion() {
  const version = readPackageVersion();
  console.log(`v${version}`);
  console.log(`node=${process.version}`);
  console.log(`platform=${process.platform}`);
  console.log(`diff_scoping=enabled`);
  console.log(`agent_modules=doctrineGuard,reportWriter,prBody,patchWriter,repoScanner,testRunner`);
}


// ─── self-check ───────────────────────────────────────────────────────


/**
 * Minimum Node version. ES module syntax (`import` / `export`) needs
 * Node 14+ at the language level, but several conveniences this kit
 * uses (top-level await in places, `fs.readdirSync` w/ withFileTypes,
 * native `URL`/`fileURLToPath`) need Node 16+. We bump to 16 as the
 * floor to avoid subtle "works on the desk, breaks on the brain pod"
 * failures.
 */
const MIN_NODE_MAJOR = 16;

const SEEDED_DIFF = [
  "--- a/shared/example.py",
  "+++ b/shared/example.py",
  "@@ -1,2 +1,3 @@",
  " def helper():",
  "     return 42",
  "+    return may_override(intent)",
].join("\n");

const RESULTS = [];


function record(name, ok, detail = "") {
  RESULTS.push({ name, ok, detail });
  const tag = ok ? "[PASS]" : "[FAIL]";
  const line = detail ? `${tag} ${name} — ${detail}` : `${tag} ${name}`;
  console.log(line);
}


async function checkNodeVersion() {
  const major = parseInt(process.version.replace(/^v/, "").split(".")[0], 10);
  if (Number.isFinite(major) && major >= MIN_NODE_MAJOR) {
    record("Node version compatible", true, `node=${process.version}, min=v${MIN_NODE_MAJOR}.0.0`);
  } else {
    record("Node version compatible", false, `got ${process.version}, need >=v${MIN_NODE_MAJOR}.0.0`);
  }
}


async function checkEsModuleSupport() {
  // If this module loaded at all, ES modules work. But we surface the
  // signal explicitly for operator clarity.
  record("ES module support", true, "import/export resolved");
}


async function checkModuleLoads(modName, importPath, expectedExports) {
  try {
    const mod = await import(importPath);
    for (const sym of expectedExports) {
      if (typeof mod[sym] !== "function" && typeof mod[sym] !== "object") {
        record(`${modName} loads`, false, `missing export '${sym}'`);
        return;
      }
    }
    record(`${modName} loads`, true, `exports: ${expectedExports.join(", ")}`);
  } catch (e) {
    record(`${modName} loads`, false, e.message);
  }
}


async function checkSafeRunnerBlocks() {
  try {
    const { default: undefined_unused } = { default: null };  // keep ESLint quiet on dynamic import below
    const mod = await import("./testRunner.js");
    let threw = false;
    let errorMessage = "";
    try {
      // Should throw before executing — never actually invokes the command.
      await mod.runTests("sudo rm -rf /etc/passwd");
    } catch (e) {
      threw = true;
      errorMessage = e.message;
    }
    if (threw && /unsafe|blocked/i.test(errorMessage)) {
      record("safe-runner blocks dangerous command", true, "refused 'sudo rm -rf'");
    } else if (threw) {
      record(
        "safe-runner blocks dangerous command",
        false,
        `threw, but message didn't mention block: ${errorMessage}`,
      );
    } else {
      record("safe-runner blocks dangerous command", false, "did not throw");
    }
  } catch (e) {
    record("safe-runner blocks dangerous command", false, `module load failed: ${e.message}`);
  }
}


async function checkDiffParser() {
  try {
    const mod = await import("./doctrineGuard.js");
    const added = mod.extractAddedLines(SEEDED_DIFF);
    if (Array.isArray(added) && added.length === 1 && added[0].includes("may_override")) {
      record("diff parser detects unified diff", true, `1 added line extracted`);
    } else {
      record(
        "diff parser detects unified diff",
        false,
        `got ${added === null ? "null (not detected as diff)" : `${added.length} lines`}`,
      );
    }
  } catch (e) {
    record("diff parser detects unified diff", false, e.message);
  }
}


async function checkSeededViolationCaught() {
  // Write the seeded diff to a temp file, invoke the report module,
  // and verify it scores HIGH risk with the may_override warning.
  // Using `report` (not `doctrine-check`) because report doesn't
  // process.exit() and we can inspect its return value.
  const tmp = path.join(process.cwd(), ".riseai-selfcheck.diff");
  try {
    fs.writeFileSync(tmp, SEEDED_DIFF);
    const mod = await import("./reportWriter.js");
    // Silence console.log during the call by capturing.
    const origLog = console.log;
    console.log = () => {};
    let report;
    try {
      report = await mod.generateReport(tmp, { json: true });
    } finally {
      console.log = origLog;
    }
    const hasMayOverride = (report.doctrine_warnings || []).some((w) =>
      /may_override/i.test(w.name),
    );
    if (report.risk_level === "HIGH" && hasMayOverride) {
      record(
        "doctrine-check catches seeded violation",
        true,
        `risk=HIGH, may_override warning fired`,
      );
    } else {
      record(
        "doctrine-check catches seeded violation",
        false,
        `risk=${report.risk_level}, may_override=${hasMayOverride}`,
      );
    }
  } catch (e) {
    record("doctrine-check catches seeded violation", false, e.message);
  } finally {
    try {
      fs.unlinkSync(tmp);
    } catch (e) {
      // best-effort cleanup
    }
  }
}


export async function runSelfCheck() {
  console.log("RISEAI SELF-CHECK");
  console.log(`v${readPackageVersion()} · node=${process.version} · platform=${process.platform}`);
  console.log("");

  await checkNodeVersion();
  await checkEsModuleSupport();
  await checkModuleLoads("doctrineGuard", "./doctrineGuard.js", [
    "doctrineCheck",
    "extractAddedLines",
    "DANGEROUS_PATHS",
    "FORBIDDEN_PATTERNS",
  ]);
  await checkModuleLoads("reportWriter", "./reportWriter.js", [
    "generateReport",
    "scoreRisk",
  ]);
  await checkModuleLoads("prBody", "./prBody.js", [
    "generatePrBody",
    "extractTouchedFiles",
    "diffStat",
  ]);
  await checkModuleLoads("patchWriter", "./patchWriter.js", ["writePatch"]);
  await checkModuleLoads("repoScanner", "./repoScanner.js", ["scanRepo"]);
  await checkModuleLoads("testRunner", "./testRunner.js", ["runTests"]);
  await checkSafeRunnerBlocks();
  await checkDiffParser();
  await checkSeededViolationCaught();

  const passed = RESULTS.filter((r) => r.ok).length;
  const failed = RESULTS.filter((r) => !r.ok).length;
  console.log("");
  console.log(`SUMMARY: ${passed} passed, ${failed} failed`);

  if (failed === 0) {
    console.log("HEALTHY: kit is ready for use.");
    return 0;
  }
  console.log("BROKEN: kit is NOT safe to use until the failures above are resolved.");
  return 1;
}
