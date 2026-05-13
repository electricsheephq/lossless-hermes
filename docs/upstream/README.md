# Upstream Hermes-agent Patches

Tracks the 4 additive ABC patches proposed to `NousResearch/hermes-agent` per [ADR-015](../adr/015-hermes-upstream-patches.md). Each patch has its own status file.

## Patches

| ID | Title | ADR | Status | PR URL | Blocks |
|---|---|---|---|---|---|
| 001 | `ContextEngine.preassemble()` | [ADR-010](../adr/010-always-on-assembly.md) | drafted | — | 03-09, 03-10 |
| 002 | `_EngineCollector.register_command` forwarding | [ADR-015](../adr/015-hermes-upstream-patches.md) #2 | drafted | — | none (cleanup) |
| 003 | `ContextEngine.ingest()` ABC method | [ADR-015](../adr/015-hermes-upstream-patches.md) #3 | drafted | — | none (cleanup) |
| 004 | Cache-token forwarding in `update_from_response(usage)` | [ADR-015](../adr/015-hermes-upstream-patches.md) #4 | drafted | — | none (graceful degrade) |

## Status lifecycle

```
drafted → filed → under_review → accepted → shipped
                          ↘ rejected → fallback
```

- **drafted**: design captured here, not yet filed upstream
- **filed**: PR opened on `NousResearch/hermes-agent` (URL captured)
- **under_review**: maintainer is engaged; may request changes
- **accepted**: PR approved, awaiting merge or merged but not in a release tag
- **shipped**: in a tagged Hermes release that lossless-hermes can require
- **rejected**: maintainer declined; ADR fallback path is what we ship

## Weekly check-in

Every Monday during Wave 4+, run:

```bash
for f in docs/upstream/0*.md; do
  pr=$(grep '^pr_url:' "$f" | awk '{print $2}')
  [ -n "$pr" ] && [ "$pr" != "—" ] && gh pr view "$pr" --json state,reviewDecision,mergedAt
done
```

Update each file's `status:` and `last_checked:` frontmatter.

## Authoring convention

Each patch file has YAML frontmatter + a body describing the patch, rationale (linked to the ADR), the fallback if rejected, and a transition log.
