# Blockers

> **Append-only dated queue of open decisions awaiting Claude judgment.**
>
> Resolved entries archive to `docs/decisions/YYYY-MM.md` after 30 days.

## Open blockers

_(none open as of 2026-05-13)_

## Resolved (archive after 30 days)

_(none yet)_

---

## Entry format

```markdown
### B-NNN — YYYY-MM-DD — <one-line title>
**Raised by:** <agent type + issue/wave>
**Blocks:** <issue IDs, comma-separated, or "wave-level">
**Question:** <what needs deciding>
**Recommended action:** <agent's recommendation>
**Decision required:** Claude (autonomous) | human escalation
**Status:** open | resolved (YYYY-MM-DD)
**Resolution:** <if resolved, what was decided + commit/PR link>
```

## Judgment-call distinction

- **BLOCKERS.md** is for "I cannot proceed without a decision from Claude."
- For "I proceeded autonomously, here's the choice I made": record in the **commit message** of the issue (e.g., `Note: chose option A because <rationale>`). Don't add to this file.
- Durable design decisions get a new ADR; BLOCKERS is the *queue*, ADRs are the *record once accepted*.
