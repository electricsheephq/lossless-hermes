/**
 * Extract LCM SQLite schema by running runLcmMigrations() against an in-memory DB.
 *
 * Usage:
 *   cd /Volumes/LEXAR/Claude/lossless-claw  # must be run with LCM as cwd
 *   npx tsx /Volumes/LEXAR/Claude/lossless-hermes/scripts/extract_lcm_schema.ts \
 *     --output /Volumes/LEXAR/Claude/lossless-hermes/tests/fixtures/lcm_reference_schema.sql
 *
 * Or via the orchestrator:
 *   /Volumes/LEXAR/Claude/lossless-hermes/scripts/schema_diff.sh --refresh-reference
 *
 * Requires:
 *   - Node 22+ (uses node:sqlite)
 *   - LCM repo at $LCM_TS_PATH with deps installed (pnpm install)
 *   - tsx for TypeScript runtime
 *
 * Output: SQL DDL one statement per logical block, lexicographically ordered
 *   (type, name) for stable diffs. Includes tables, views, indexes, triggers.
 */
import { DatabaseSync } from "node:sqlite";
import { pathToFileURL } from "node:url";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";

const LCM_ROOT = process.env.LCM_TS_PATH || "/Volumes/LEXAR/Claude/lossless-claw";
const args = process.argv.slice(2);
const outputIdx = args.indexOf("--output");
const outputPath = outputIdx >= 0 ? args[outputIdx + 1] : null;

interface MasterRow {
  type: string;
  name: string;
  tbl_name: string;
  sql: string | null;
}

async function main() {
  const migrationUrl = pathToFileURL(resolve(LCM_ROOT, "src/db/migration.ts")).href;
  const mod = (await import(migrationUrl)) as {
    runLcmMigrations: (db: DatabaseSync, opts?: { fts5Available?: boolean; seedDefaultPrompts?: boolean }) => void;
  };

  const db = new DatabaseSync(":memory:");
  // Match production defaults: FTS5 available, seed default prompts.
  // These options must mirror what runLcmMigrations is called with in src/engine.ts.
  mod.runLcmMigrations(db, { fts5Available: true, seedDefaultPrompts: true });

  // Dump all schema objects in a stable order.
  const rows = db
    .prepare(
      `SELECT type, name, tbl_name, sql
         FROM sqlite_master
        WHERE sql IS NOT NULL
          AND name NOT LIKE 'sqlite_%'
        ORDER BY type, name`,
    )
    .all() as MasterRow[];

  const blocks: string[] = [];
  blocks.push(`-- LCM reference schema, generated from runLcmMigrations()`);
  blocks.push(`-- LCM_TS_PATH: ${LCM_ROOT}`);
  blocks.push(`-- Generated: ${new Date().toISOString()}`);
  blocks.push(`-- Total objects: ${rows.length}`);
  blocks.push(``);

  for (const row of rows) {
    blocks.push(`-- ${row.type}: ${row.name}`);
    // Trim trailing whitespace, ensure terminating semicolon.
    const sql = row.sql!.trim().replace(/;\s*$/, "") + ";";
    blocks.push(sql);
    blocks.push(``);
  }

  // Also enumerate pragma values that matter for schema (FK enforcement).
  // These aren't in sqlite_master but are semantically schema.
  blocks.push(`-- pragmas`);
  for (const pragma of ["foreign_keys", "journal_mode", "synchronous", "user_version"]) {
    const result = db.prepare(`PRAGMA ${pragma}`).get() as Record<string, unknown> | undefined;
    blocks.push(`-- pragma ${pragma}: ${JSON.stringify(result)}`);
  }
  blocks.push(``);

  const output = blocks.join("\n");
  if (outputPath) {
    writeFileSync(outputPath, output, "utf8");
    console.error(`wrote ${rows.length} schema objects to ${outputPath}`);
  } else {
    process.stdout.write(output);
  }

  db.close();
}

main().catch((err) => {
  console.error("extract_lcm_schema failed:", err);
  process.exit(1);
});
