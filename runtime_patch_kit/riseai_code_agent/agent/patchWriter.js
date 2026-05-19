/**
 * Patch-note writer — operator-readable PROPOSED_ONLY note.
 *
 * Status is hardcoded to PROPOSED_ONLY. The tool will never write
 * APPROVED or DEPLOYED — those transitions are an operator decision,
 * not an agent decision.
 */
import fs from "fs";
import path from "path";

export async function writePatch(title, body) {
  if (!body || !body.trim()) {
    throw new Error(
      "Patch note body cannot be empty. A patch without a written intent is " +
        "not a proposal — it's a guess.",
    );
  }

  fs.mkdirSync("patches", { recursive: true });

  const safeTitle = title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_|_$/g, "")
    .slice(0, 60) || "untitled";

  const file = path.join("patches", `${Date.now()}_${safeTitle}.md`);

  const content = `# RISEAI Patch Note

## Title
${title}

## Intent
${body}

## Required Safety
- Run \`riseai-code doctrine-check\` on the unified diff
- Run brain-local tests via \`riseai-code test <command>\`
- No live broker execution changes without explicit operator approval
- MC tripwires remain the source of runtime truth — this note does not bypass them

## Status
PROPOSED_ONLY

## Generated
${new Date().toISOString()}
`;

  fs.writeFileSync(file, content);
  console.log(`Patch note written: ${file}`);
}
