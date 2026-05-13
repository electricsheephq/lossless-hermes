"""Per-subcommand handler tests for issue 08-01 (and subsequent 08-NN bodies).

This subpackage hosts the dispatcher / parser / owner-gating tests that the
issue 08-01 acceptance criteria require:

* ``test_dispatcher.py`` — every dispatch key routes to the right module.
* ``test_parse_lcm_command.py`` — token splitter quoting / list / flag
  invariants.
* ``test_owner_gating.py`` — destructive subcommands stay un-reached when
  the upstream :class:`SlashAccessPolicy` denies.

Subsequent issues 08-02..08-15 add per-subcommand body tests here
(``test_status_text.py``, ``test_purge.py``, etc.).
"""
