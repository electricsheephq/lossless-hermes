#!/usr/bin/env node
/**
 * Extract LCM tool description strings as a JSON fixture for the
 * verbatim-description lint (issue 06-15).
 *
 * The output is committed to
 * ``tests/fixtures/lcm_v4.1_tool_descriptions.json`` and consumed by
 * ``tests/tools/test_descriptions_verbatim.py``. The test asserts
 * byte-identical match between each registered Python tool's
 * ``description`` field and the corresponding fixture entry.
 *
 * Why a regex extractor (and not full TypeBox/TS evaluation): every
 * tool factory exports a ``description`` literal that is a
 * concatenation of "..." + "..." + "..." string literals. The pattern
 * is mechanical — we don't need a TS AST. A regex per line keeps the
 * script ~50 LOC with no Node dependencies (only ``node:fs``).
 *
 * Usage:
 *   node scripts/extract_tool_descriptions.mjs \
 *     [--lcm /Volumes/LEXAR/Claude/lossless-claw] \
 *     [--output tests/fixtures/lcm_v4.1_tool_descriptions.json]
 *
 * Requires:
 *   - Node 18+ (uses node:fs, ESM, structured JSON parsing).
 *   - LCM repo at $LCM_TS_PATH or --lcm path, on the pinned commit.
 *
 * On bumping the LCM source-map pin:
 *   1. Update ``docs/reference/lcm-source-map.md`` to the new SHA.
 *   2. Run this script, which writes the new fixture (and prints the
 *      SHA-256 per tool to stdout for easy copy-paste into the test).
 *   3. Update ``_DESCRIPTION_SHA256`` in
 *      ``tests/tools/test_descriptions_verbatim.py`` to match.
 *   4. Re-port any drifted prose into the matching Python source file
 *      (``src/lossless_hermes/tools/<name>.py``). Per ADR-016 §
 *      "Verbatim-description rule", we never relax the lint — we always
 *      re-port the prose.
 */

import { readFileSync, writeFileSync } from "node:fs";
import { execSync } from "node:child_process";
import { createHash } from "node:crypto";
import { resolve, join } from "node:path";

// ---------------------------------------------------------------------------
// CLI args
// ---------------------------------------------------------------------------

const args = process.argv.slice(2);
function flag(name, fallback) {
  const idx = args.indexOf(name);
  return idx >= 0 ? args[idx + 1] : fallback;
}
const LCM_ROOT = resolve(
  flag("--lcm", process.env.LCM_TS_PATH || "/Volumes/LEXAR/Claude/lossless-claw"),
);
const SCRIPT_DIR = new URL(".", import.meta.url).pathname;
const REPO_ROOT = resolve(SCRIPT_DIR, "..");
const OUTPUT_PATH = resolve(
  flag(
    "--output",
    join(REPO_ROOT, "tests/fixtures/lcm_v4.1_tool_descriptions.json"),
  ),
);

// ---------------------------------------------------------------------------
// Tool catalog — every TS tool factory + the registered ``name:`` literal
// ---------------------------------------------------------------------------
//
// Order matches the fixture's intentional ordering: the seven v0.1.0
// tools first, then lcm_expand_query (deferred per ADR-012 but included
// for completeness so a future un-defer is mechanical).

const TOOL_CATALOG = [
  { name: "lcm_grep", file: "src/tools/lcm-grep-tool.ts" },
  { name: "lcm_describe", file: "src/tools/lcm-describe-tool.ts" },
  { name: "lcm_expand", file: "src/tools/lcm-expand-tool.ts" },
  {
    name: "lcm_synthesize_around",
    file: "src/tools/lcm-synthesize-around-tool.ts",
  },
  { name: "lcm_get_entity", file: "src/tools/lcm-get-entity-tool.ts" },
  { name: "lcm_search_entities", file: "src/tools/lcm-search-entities-tool.ts" },
  { name: "lcm_compact", file: "src/tools/lcm-compact-tool.ts" },
  { name: "lcm_expand_query", file: "src/tools/lcm-expand-query-tool.ts" },
];

// ---------------------------------------------------------------------------
// Extractor — regex per line over the TS source
// ---------------------------------------------------------------------------
//
// Pattern: a line ``description:`` followed by N indented string-literal
// lines each ending with ``+`` (continuation) or ``,`` (close). The
// extractor joins the literals after running each through ``JSON.parse``
// to handle escapes (\n, \", \\, etc.) — TS string-literal escape rules
// are a superset of JSON's, so this is safe for the description
// surface (no template literals, no tagged templates).

function extractDescription(filePath, toolName) {
  const lines = readFileSync(filePath, "utf-8").split("\n");
  // Locate ``name: "<toolName>"`` line as the anchor.
  let i = lines.findIndex((l) => l.includes(`name: "${toolName}"`));
  if (i < 0) {
    throw new Error(`name: "${toolName}" not found in ${filePath}`);
  }
  // Walk forward to ``    description:`` line.
  while (i < lines.length && !/^\s+description:/.test(lines[i])) {
    i++;
  }
  if (i >= lines.length) {
    throw new Error(
      `description: line not found after name: "${toolName}" in ${filePath}`,
    );
  }
  // The first literal line is i+1.
  i++;
  const literalStart = i + 1; // 1-based for human-readable provenance
  const parts = [];
  let literalEnd = i + 1;
  while (i < lines.length) {
    const line = lines[i].trim();
    const m = line.match(/^"((?:[^"\\]|\\.)*)"\s*([+,])\s*$/);
    if (!m) {
      throw new Error(
        `Could not parse description literal at line ${i + 1} of ${filePath}: ${JSON.stringify(line)}`,
      );
    }
    parts.push(m[1]);
    literalEnd = i + 1;
    if (m[2] === ",") {
      break;
    }
    i++;
  }
  // Re-parse each literal segment through JSON.parse to handle escapes
  // (the TS string-literal escape grammar is a superset of JSON's; this
  // is correct for the description surface — no template literals, no
  // tagged templates, no octal escapes).
  const description = parts.map((s) => JSON.parse(`"${s}"`)).join("");
  return { description, lineStart: literalStart, lineEnd: literalEnd };
}

// ---------------------------------------------------------------------------
// Git commit SHA for provenance
// ---------------------------------------------------------------------------

function readLcmCommitSha() {
  try {
    return execSync("git rev-parse HEAD", {
      cwd: LCM_ROOT,
      encoding: "utf-8",
    }).trim();
  } catch (e) {
    throw new Error(
      `Failed to read LCM commit SHA from ${LCM_ROOT}. ` +
        `Check that the LCM repo is checked out and on the pinned commit. ` +
        `Underlying error: ${e.message}`,
    );
  }
}

function readLcmBranch() {
  try {
    return execSync("git rev-parse --abbrev-ref HEAD", {
      cwd: LCM_ROOT,
      encoding: "utf-8",
    }).trim();
  } catch {
    return "unknown";
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

function isoDate() {
  return new Date().toISOString().slice(0, 10);
}

function main() {
  const sha = readLcmCommitSha();
  const branch = readLcmBranch();

  const extractedFrom = {};
  const descriptions = {};
  for (const { name, file } of TOOL_CATALOG) {
    const abs = join(LCM_ROOT, file);
    const { description, lineStart, lineEnd } = extractDescription(abs, name);
    extractedFrom[name] = `${file}:${lineStart}-${lineEnd}`;
    descriptions[name] = description;
  }

  const fixture = {
    _provenance: `lossless-claw@${sha}`,
    _branch: branch,
    _extracted_at: isoDate(),
    _extracted_from: extractedFrom,
    ...descriptions,
  };

  // Write the fixture (pretty-printed, trailing newline for POSIX
  // and to satisfy pre-commit end-of-file-fixer).
  writeFileSync(OUTPUT_PATH, JSON.stringify(fixture, null, 2) + "\n", "utf-8");

  // Emit SHA-256 per tool to stdout for easy copy-paste into
  // tests/tools/test_descriptions_verbatim.py._DESCRIPTION_SHA256.
  console.log(`wrote ${OUTPUT_PATH}`);
  console.log("");
  console.log("SHA-256 per description (copy into _DESCRIPTION_SHA256):");
  for (const { name } of TOOL_CATALOG) {
    const h = createHash("sha256")
      .update(descriptions[name], "utf-8")
      .digest("hex");
    console.log(`  "${name}": "${h}",`);
  }
}

main();
