/**
 * Test runner — refuses commands containing known-dangerous shell
 * primitives. The agent's promise: this script will NEVER let the
 * brain agent accidentally `sudo` or `rm -rf` while iterating on a
 * patch.
 *
 * The forbidden list is a denylist, not an allowlist. It catches
 * obvious foot-guns but does NOT replace operator review of unusual
 * test commands. If you find yourself wanting to add something to
 * the denylist, ask first whether the command should run at all.
 */
import { execSync } from "child_process";

const FORBIDDEN = [
  "rm -rf",
  "rm  -rf",
  "sudo",
  "curl ",
  "wget ",
  "scp ",
  "ssh ",
  "docker run",
  "docker exec",
  "kubectl",
  "terraform apply",
  "terraform destroy",
  // git write actions — operator should run these by hand
  "git push",
  "git reset --hard",
  "git rebase",
  // mongo / db wipes
  "drop database",
  "dropDatabase",
  ".drop(",
  // env/secret exfil
  "cat /etc/",
  "cat ~/.ssh",
  "printenv",
];

export async function runTests(command) {
  if (!command || !command.trim()) {
    throw new Error("Missing test command. Usage: riseai-code test <command>");
  }

  const lower = command.toLowerCase();
  for (const bad of FORBIDDEN) {
    if (lower.includes(bad.toLowerCase())) {
      throw new Error(
        `Unsafe test command blocked: contains "${bad}". ` +
          `RISEAI test runner refuses commands that mutate the host or ` +
          `network. If this is intentional, run it by hand and document it.`,
      );
    }
  }

  console.log(`Running: ${command}`);
  try {
    execSync(command, {
      stdio: "inherit",
      shell: true,
    });
  } catch (e) {
    console.log("RISEAI TEST RESULT: FAIL");
    process.exit(typeof e.status === "number" ? e.status : 1);
  }

  console.log("RISEAI TEST RESULT: PASS");
}
