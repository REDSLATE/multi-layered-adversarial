# RISEAI Code Agent v0.6

Brain-side guardrail for RISEDUAL sidecar repos. Pre-PR safety checks
that catch obvious doctrine violations BEFORE a patch lands and BEFORE
MC's runtime gates have to reject the resulting behavior.

## Doctrine split

```
Brain agent proposes patch
        ↓
riseai-code doctrine-check patch.diff        ← THIS TOOL (grep tripwire)
        ↓
riseai-code test "pytest tests/"             ← THIS TOOL (safe runner)
        ↓
riseai-code patch-note "title" "body"        ← THIS TOOL (operator note)
        ↓
PR review / brain redeploy
        ↓
MC runtime gates accept/reject behavior      ← SOURCE OF TRUTH
```

**This tool is NOT MC enforcement.** MC's tripwire pytest suite
(`pytest -m tripwire`, 116 passing) plus the doctrine-locked modules
(`council.py`, `auto_router.py`, `broker_router.py`, `platform_survival.py`)
are the runtime enforcement of record. This tool is the cheap upstream
check that catches "I accidentally touched the broker router" before
the patch ever leaves the brain repo.

False positives are possible — when one fires, read the message, decide
whether the change is intentional, and document the override in the
patch note. The tool blocks; the operator decides.

## Install (in a brain repo)

```bash
cp -r runtime_patch_kit/riseai_code_agent ./riseai-code-agent
cd riseai-code-agent
npm install
npm link
riseai-code self-check    # ← verify install before relying on the tool
riseai-code help
```

## self-check — installation health diagnostic (v0.5)

After `npm link`, run `riseai-code self-check`. The tool exercises
every agent module against a synthetic input — no network, no
filesystem writes beyond a temp diff string. Output:

```
RISEAI SELF-CHECK
v0.6.0 · node=v20.20.2 · platform=linux

[PASS] Node version compatible — node=v20.20.2, min=v16.0.0
[PASS] ES module support — import/export resolved
[PASS] doctrineGuard loads — exports: doctrineCheck, extractAddedLines, ...
[PASS] reportWriter loads — exports: generateReport, scoreRisk
[PASS] prBody loads — exports: generatePrBody, extractTouchedFiles, diffStat
[PASS] patchWriter loads — exports: writePatch
[PASS] repoScanner loads — exports: scanRepo
[PASS] testRunner loads — exports: runTests
[PASS] llmProvider loads — exports: callLLM, defaultModel, resolveApiKey, PROVIDERS
[PASS] diagnose loads — exports: runDiagnose, extractDiff
[PASS] safe-runner blocks dangerous command — refused 'sudo rm -rf'
[PASS] diff parser detects unified diff — 1 added line extracted
[PASS] doctrine-check catches seeded violation — risk=HIGH, may_override warning fired

SUMMARY: 13 passed, 0 failed
HEALTHY: kit is ready for use.
```

Exit codes:
- `0` — kit is healthy, safe to use
- `1` — kit is broken, do NOT use until failures are resolved

The CLI uses lazy imports so `self-check` and `version` work even
when other modules are missing or broken — a hard prerequisite for
diagnosing a broken install.

## version — kit + runtime fingerprint

```bash
riseai-code version
```

Output:

```
v0.6.0
node=v20.20.2
platform=linux
diff_scoping=enabled
agent_modules=doctrineGuard,reportWriter,prBody,patchWriter,repoScanner,testRunner,llmProvider,diagnose
```

Use this when comparing two brain agents' behavior on the same diff
— if the version line differs, the answer is "you're on different
kit versions" before you start debugging anything else.

## Commands

| Command | Purpose | Exit behavior |
|---|---|---|
| `riseai-code self-check` | Installation health diagnostic | **1 if broken, 0 if healthy** |
| `riseai-code version` | Kit version + runtime context | 0 always |
| `riseai-code scan <path>` | Walk repo, list inspectable files | 0 always |
| `riseai-code doctrine-check <file>` | Grep tripwire on a unified diff | **2 on match (BLOCKS)** |
| `riseai-code report <file> [--json]` | Structured patch review | **0 always (REVIEW MATERIAL)** |
| `riseai-code pr-body <file> [--title <name>]` | Ready-to-paste PR description | **0 always (COMPOSER)** |
| `riseai-code patch-note "title" "body"` | Write PROPOSED_ONLY operator note | 0 always |
| `riseai-code test <command>` | Safe test runner; refuses dangerous shell | inherits from test |
| `riseai-code diagnose <question> [opts]` | LLM patch proposer (writes review files; never applies) | **0 healthy, 1 usage, 2 LLM failure** |

The three diff-aware commands share the same grep engine but have
distinct contracts:

| Command | Role | Pasted into |
|---|---|---|
| `doctrine-check` | Gate — enforces discipline | CI / pre-commit |
| `report` | Reviewer — helps iteration | Terminal scratchpad |
| `pr-body` | Composer — operator review surface | The PR description |

## pr-body — bridge between brain-coding and operator-governance (v0.4)

```bash
riseai-code pr-body /tmp/patch.diff --title "spread snapshot fallback"
```

Generates a markdown body the brain agent pastes directly into the PR.
The body contains:

- Risk badge (LOW / MEDIUM / HIGH) + diffstat + one-line summary
- Files touched
- Doctrine review (surfaces + warnings)
- Recommended tests
- **Rollout checklist** — scales with risk; LOW gets 3 items, HIGH gets 7
- **Rollback checklist** — scales with risk; LOW gets 2 items, HIGH gets up to 8 including broker-state-drift audit and execution-receipt cross-check
- Intent placeholder (brain agent fills this in)
- Footer reminding the reviewer that MC tripwires remain runtime truth

The risk tier governs the ceremony: a tiny patch doesn't bury the
reviewer in checklist items; a broker_router touch makes them
acknowledge the full audit trail before merge.

## report — structured patch review (v0.3)

```bash
riseai-code report /tmp/patch.diff
```

Output (YAML-ish, pipeable):

```yaml
risk_level: HIGH
scan_mode: DIFF
added_lines: 5
touched_surfaces:
  - broker_router
doctrine_warnings:
  - name: may_override re-introduction
    message: Added code references `may_override`. This field was removed from
             doctrine on 2026-02-19 (4-seat merge). Do not re-introduce.
recommended_tests:
  - pytest -k broker_router -v
  - pytest -m tripwire
operator_summary: 5 added lines; 1 doctrine warning and touches broker_router. Operator approval required before merge.
```

Pass `--json` for machine consumption:

```bash
riseai-code report /tmp/patch.diff --json | jq .risk_level
```

### Risk scoring

| Level | Trigger |
|---|---|
| `LOW` | No sensitive surfaces touched, no patterns fired |
| `MEDIUM` | Touches a sensitive but non-load-bearing surface (e.g. `live_position`, `exposure_cap`) with no forbidden patterns |
| `HIGH` | Any forbidden pattern fires, OR any load-bearing surface touched (`broker_router`, `kill_switch`, `roadguard`, `executor_seat`, `auto_router`, `execution_authority`, `pdt`, `broker_adapter`) |

### Recommended tests

Each touched surface maps to a suggested `pytest -k <keyword>`
invocation. HIGH-risk patches also recommend the full
`pytest -m tripwire` suite as a backstop.

These are **suggestions**, not authority. MC's tripwire suite remains
the source of truth.

## doctrine-check — the v0.2 diff-scoping

v0.1 scanned the entire file content, which false-positived on
operator documentation. **v0.2 parses unified diff format and runs
patterns ONLY against added lines** (`+` lines, not `+++` headers,
not removed `-` lines, not unchanged context).

Generate a diff and check it:

```bash
git diff > /tmp/patch.diff
riseai-code doctrine-check /tmp/patch.diff
```

Exit code:
- `0` — pass; safe to merge from doctrine perspective
- `2` — blocked; review the listed warnings
- `1` — usage error

### Patterns the tool watches

```text
Dangerous surfaces (any mention in added code triggers a warning):
  broker_router, broker_adapter, live_position(s), roadguard, pdt,
  kill_switch, exposure_cap, executor_seat, auto_router,
  execution_authority

Forbidden patterns:
  HOLD promotion          HOLD mixed with BUY/SELL/SHORT/COVER
  Council direction       Council touching direction (size only allowed)
  RoadGuard bypass        skip/bypass/approve flips on RoadGuard
  Operator gate ON        Default-ON execution gates
  may_override            Re-introduction of the deleted field
  Decider/advisor names   Use canonical 4-seat names
```

### When the input isn't a diff

If you pass a raw `.py` or `.md` file (no `---`/`+++` headers), the
tool falls back to whole-file scan and emits:

```
RISEAI DOCTRINE CHECK: PASS|BLOCKED
  scan mode: WHOLE-FILE (no diff headers found — false positives likely)
```

Use this mode only when you want to grep the doctrine surface of a
file you didn't author. For patches, always pass a diff.

## diagnose — LLM patch proposer (v0.6)

```bash
riseai-code diagnose "spread guard rejects when bps_field missing" \
    --paths backend/shared/execution.py,backend/shared/roadguard.py \
    --provider anthropic
```

Reads the listed repo files, sends them with the operator's question to
the chosen LLM provider, and writes a proposal to disk for review. It
**never applies** a patch — output is review material only.

### Provider portability (the "leave-the-platform" story)

`diagnose` calls each provider's PUBLIC HTTPS endpoint directly. No
Emergent-platform-specific broker, no Python shell-out, no extra npm
package. The only thing that changes when you self-host this kit is
which API key env var is populated:

| `--provider` | Endpoint                                                        | API key env var      |
|--------------|-----------------------------------------------------------------|----------------------|
| `anthropic`  | `https://api.anthropic.com/v1/messages`                         | `ANTHROPIC_API_KEY`  |
| `openai`     | `https://api.openai.com/v1/chat/completions`                    | `OPENAI_API_KEY`     |
| `gemini`     | `https://generativelanguage.googleapis.com/v1beta/.../generateContent` | `GEMINI_API_KEY`   |

The Emergent Universal LLM Key is **not** supported by this CLI on
purpose — it's a Python-only broker and the kit is built for life
after the platform. If you're still on Emergent, run your LLM work
through the backend's Python `emergentintegrations` paths and use
`diagnose` with a direct provider key.

### Flags

| Flag | Default | Purpose |
|---|---|---|
| `--provider` | `anthropic` | One of `anthropic`, `openai`, `gemini` |
| `--model` | provider-recommended | Override the model id |
| `--paths` | **REQUIRED** | Comma-separated repo file paths to include as context |
| `--out` | `./riseai-proposals` | Output directory |
| `--max-bytes` | `50000` | Per-file size cap (truncation marker added if hit) |
| `--max-tokens` | `4096` | LLM response cap |

### Output

Two files per run, slugified from the question:

```
<out>/<UTC-timestamp>-<slug>.md      # Full proposal (analysis + patch + tests + rollback + risk)
<out>/<UTC-timestamp>-<slug>.patch   # Extracted unified diff (omitted if model proposed NONE)
```

Recommended flow after a proposal lands:

```bash
riseai-code diagnose "..." --paths ...        # write proposal
riseai-code doctrine-check <out>/...patch     # grep tripwire on the proposed diff
riseai-code report <out>/...patch             # structured patch review
git apply --check <out>/...patch              # operator validates apply
```

### What the LLM sees

The system prompt locks the model into RISEDUAL doctrine:
- MC is a notary; never veto trade quality.
- Governor sizes, RoadGuard caps, Opponent vetoes, Camaro routes.
- Memory provenance is strict (VE/SO/DI/UV; only VE trains).
- Role anchors are fixed and not negotiable.
- Tripwires are sacred; if a proposed patch would break one, the
  Analysis section must call it out.

Output is strictly a five-section markdown: Analysis / Proposed Patch /
Tests / Rollback / Risk. No preamble, no chat.

### Why no auto-walk of the repo?

`--paths` is required. The CLI refuses to walk the repo and decide
which files matter, because:
1. Context-window honesty — the operator KNOWS what got sent.
2. Cost honesty — you pay per token; you choose what burns.
3. Doctrine — silent over-inclusion of files turns the proposer into
   a black box. Picking paths is operator discipline.

---

## What this tool does NOT do

- **No silent model auto-application.** `diagnose` writes proposals to
  disk; it never applies a patch on its own. Operator reviews + runs
  `doctrine-check` on the extracted patch before any apply.
- **No MC authority.** Passing `doctrine-check` says nothing about
  whether MC will accept the resulting behavior. The receipt seal,
  the seat policy, the snapshot contract — those all live on MC and
  enforce themselves at runtime.
- **No git operations.** The test runner refuses `git push`, `git
  reset --hard`, `git rebase`. Those are operator actions.
- **No production calls.** The test runner refuses `curl`, `wget`,
  `ssh`, `docker run`, `kubectl`, `terraform apply`. If a test
  requires a network call, it should mock the call.

## Where this lives

`/runtime_patch_kit/riseai_code_agent/` in the MC repo. Brain agents
pull from here when they want to wire it into their own repos.

This tool is **not deployed to MC's backend.** It is a CLI used by
brain repos in pre-merge workflows.
