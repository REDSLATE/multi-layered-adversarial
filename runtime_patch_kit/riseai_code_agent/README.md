# RISEAI Code Agent v0.3

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
riseai-code help
```

## Commands

| Command | Purpose | Exit behavior |
|---|---|---|
| `riseai-code scan <path>` | Walk repo, list inspectable files | 0 always |
| `riseai-code doctrine-check <file>` | Grep tripwire on a unified diff | **2 on match (BLOCKS)** |
| `riseai-code report <file> [--json]` | Structured patch review | **0 always (REVIEW MATERIAL)** |
| `riseai-code patch-note "title" "body"` | Write PROPOSED_ONLY operator note | 0 always |
| `riseai-code test <command>` | Safe test runner; refuses dangerous shell | inherits from test |

`doctrine-check` is the **gate** — meant to be wired into pre-commit
hooks or CI so a violating patch can't merge. `report` is the
**reviewer** — meant for the brain agent to read before opening a PR
and for the operator to read while reviewing one. They share the
same grep machinery but have opposite contracts: one blocks, one
informs.

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

## What this tool does NOT do

- **No model integration.** This is the guardrail layer, not the
  reasoning layer. A future v0.3 may add `diagnose <question>` that
  reads selected repo files and sends context to a model, but v0.2
  stays pure-grep on purpose: predictable, fast, no external deps.
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
