"""Doctor subsystem — port of LCM v4.1 ``lcm-doctor-*.ts`` (Epic 08-06/07/08).

This package consolidates the three TS plugin doctor modules:

* ``contract.py`` — canonical Pydantic models + constants shared by both
  ``apply.py`` (issue 08-07) and ``cleaners.py`` (issue 08-08).
* ``shared.py`` — :func:`detect_doctor_marker`, :func:`load_doctor_targets`,
  :func:`get_doctor_summary_stats` (this issue: 08-06).
* ``apply.py`` — :func:`apply_scoped_doctor_repair` (issue 08-07).
* ``cleaners.py`` — :func:`scan_doctor_cleaners`, :func:`apply_doctor_cleaners`
  (issue 08-08).

Per ``docs/porting-guides/doctor-ops.md`` §"Doctor contract API (canonical)"
line 31: "No file named ``doctor-contract-api.d.ts`` exists in the
lossless-claw tree on ``pr-613``. The 'formal contract' is the union of
exported types and functions across the three plugin doctor modules."

The Python port consolidates the canonical types in ``contract`` so 08-07
and 08-08 don't accidentally diverge on the contract.
"""

from __future__ import annotations
