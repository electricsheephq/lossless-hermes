"""Per-subcommand handler modules for the ``/lcm`` slash command.

Each module here exposes one or more ``run_*(parsed: ParsedLcmCommand)``
functions consumed by :class:`lossless_hermes.plugin.commands.LcmCommandDispatcher`'s
dispatch table.

Issue 08-01 ships the **router** plus the ``status`` + ``help`` bodies plus
stubs for the other 15 subcommands. Issues 08-02 through 08-15 fill in
each stub with its real body, importing operator-side helpers from
``lossless_hermes.operator`` and store-side helpers from
``lossless_hermes.store``.

Owner-gating per ADR-013 lives upstream in Hermes — these modules trust
that ``raw_args`` reached them only after the policy gate passed. The
``(admin)`` marker in ``/lcm help`` is operator-facing documentation of
the expected ``allow_admin_from`` config.

See:

* ``docs/adr/013-owner-gating.md`` — handlers don't gate; policy does.
* ``docs/adr/024-project-layout.md`` — package layout.
* ``epics/08-cli-ops/08-01-slash-command-router.md`` — this issue.
"""
