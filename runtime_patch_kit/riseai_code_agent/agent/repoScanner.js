/**
 * Repo scanner — walks the working tree, lists files the agent
 * could reason about. Read-only; never opens file contents.
 *
 * Doctrine: skip vendored / generated / VCS dirs that would explode
 * the output. The ALLOWED_EXT set is whitelist-only — binary blobs
 * and lockfiles are intentionally not listed, because if the agent
 * doesn't know how to read them, it shouldn't pretend to scan them.
 */
import fs from "fs";
import path from "path";

const IGNORE = new Set([
  "node_modules",
  ".git",
  "__pycache__",
  ".venv",
  "venv",
  "dist",
  "build",
  ".next",
  ".pytest_cache",
  ".ruff_cache",
  ".mypy_cache",
]);

const ALLOWED_EXT = new Set([
  ".py",
  ".js",
  ".ts",
  ".jsx",
  ".tsx",
  ".json",
  ".md",
  ".yml",
  ".yaml",
  ".sh",
  ".toml",
]);

export async function scanRepo(root) {
  if (!fs.existsSync(root)) {
    throw new Error(`Path does not exist: ${root}`);
  }

  const files = [];

  function walk(dir) {
    let entries;
    try {
      entries = fs.readdirSync(dir);
    } catch (e) {
      // Unreadable dir — skip silently rather than crash the whole scan.
      return;
    }
    for (const item of entries) {
      if (IGNORE.has(item)) continue;

      const full = path.join(dir, item);
      let stat;
      try {
        stat = fs.statSync(full);
      } catch (e) {
        continue;
      }

      if (stat.isDirectory()) {
        walk(full);
      } else if (ALLOWED_EXT.has(path.extname(full))) {
        files.push(full);
      }
    }
  }

  walk(root);

  console.log("RISEAI SCAN COMPLETE");
  console.log(`Files found: ${files.length}`);

  for (const file of files.slice(0, 200)) {
    console.log("-", file);
  }

  if (files.length > 200) {
    console.log(`...and ${files.length - 200} more`);
  }
}
