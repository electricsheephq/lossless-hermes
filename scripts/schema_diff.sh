#!/usr/bin/env bash
#
# Schema-diff orchestrator for lossless-hermes.
#
# Verifies that Python lossless_hermes.db.run_lcm_migrations() produces a
# byte-compatible schema to the TS-side runLcmMigrations() reference.
#
# Modes:
#   --refresh-reference  Run TS migrations, dump schema, overwrite the committed
#                        reference fixture at tests/fixtures/lcm_reference_schema.sql.
#                        Run this when bumping the LCM_TS_REF pin.
#
#   --verify             Run Python migrations, dump schema, diff against the
#                        committed reference. Exits 0 if zero diff, 1 if delta.
#                        This is what CI runs on every PR that touches db/migration*.
#
#   --check-reference    Re-extract the TS schema and diff against the committed
#                        reference (no Python). Exits 0 if reference is fresh,
#                        1 if stale (i.e., committed reference != current TS source).
#                        Useful as a separate CI gate to catch missing refresh.
#
# Environment:
#   LCM_TS_PATH   Path to lossless-claw TS source (default /Volumes/LEXAR/Claude/lossless-claw)
#   LCM_TS_REF    Git ref to pin LCM to (default 1f07fbd; advisory, not enforced by this script)
#   PYTHON        Python interpreter (default python3)
#   TSX           tsx command (default "npx tsx")
#
# Exit codes:
#   0   schema matches (or scaffold check passed)
#   1   schema mismatch (CI must fail)
#   2   Python migrations not yet implemented (Wave 0/Wave 1 expected state)
#   3   TS migrations not runnable (LCM deps not installed or build broken)
#   4   bad args / unknown mode

set -euo pipefail

LCM_TS_PATH="${LCM_TS_PATH:-/Volumes/LEXAR/Claude/lossless-claw}"
LCM_TS_REF="${LCM_TS_REF:-1f07fbd}"
PYTHON="${PYTHON:-python3}"
TSX="${TSX:-npx tsx}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXTRACT_TS="${REPO_ROOT}/scripts/extract_lcm_schema.ts"
EXTRACT_PY="${REPO_ROOT}/scripts/extract_python_schema.py"
REFERENCE_FIXTURE="${REPO_ROOT}/tests/fixtures/lcm_reference_schema.sql"
REFERENCE_META="${REPO_ROOT}/tests/fixtures/lcm_reference_meta.txt"

usage() {
  sed -n '/^# Schema-diff/,/^set -euo pipefail/p' "$0" | head -n -2
  exit 4
}

[ "$#" -lt 1 ] && usage

MODE="$1"; shift || true

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

verify_lcm_ready() {
  if [ ! -d "$LCM_TS_PATH" ]; then
    echo "❌ LCM_TS_PATH does not exist: $LCM_TS_PATH" >&2
    exit 3
  fi
  if [ ! -d "$LCM_TS_PATH/node_modules" ]; then
    echo "❌ LCM deps not installed. Run:" >&2
    echo "      cd $LCM_TS_PATH && pnpm install" >&2
    exit 3
  fi
}

extract_ts_schema() {
  local out="$1"
  verify_lcm_ready
  (
    cd "$LCM_TS_PATH"
    $TSX "$EXTRACT_TS" --output "$out"
  )
}

extract_py_schema() {
  local out="$1"
  set +e
  "$PYTHON" "$EXTRACT_PY" --output "$out"
  local rc=$?
  set -e
  return $rc
}

write_meta() {
  local out="$1"
  local lcm_commit
  lcm_commit="$(cd "$LCM_TS_PATH" && git rev-parse HEAD 2>/dev/null || echo unknown)"
  cat > "$out" <<EOF
lcm_ts_path: $LCM_TS_PATH
lcm_ts_ref_pin: $LCM_TS_REF
lcm_ts_commit_actual: $lcm_commit
generated_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)
generator: scripts/schema_diff.sh --refresh-reference
EOF
}

# ----------------------------------------------------------------------------
# Modes
# ----------------------------------------------------------------------------

case "$MODE" in
  --refresh-reference)
    mkdir -p "$(dirname "$REFERENCE_FIXTURE")"
    echo "Extracting TS schema from $LCM_TS_PATH..." >&2
    extract_ts_schema "$REFERENCE_FIXTURE"
    write_meta "$REFERENCE_META"
    echo "✅ Refreshed reference:" >&2
    echo "      $REFERENCE_FIXTURE" >&2
    echo "      $REFERENCE_META" >&2
    ;;

  --verify)
    if [ ! -f "$REFERENCE_FIXTURE" ]; then
      echo "❌ Reference fixture missing: $REFERENCE_FIXTURE" >&2
      echo "   Run: $0 --refresh-reference" >&2
      exit 1
    fi
    tmp_py_schema="$(mktemp)"
    trap 'rm -f "$tmp_py_schema"' EXIT
    extract_py_schema "$tmp_py_schema"
    py_rc=$?
    if [ $py_rc -eq 2 ]; then
      # Wave 0 / Wave 1 expected state
      echo "⚠️  Python migrations not yet implemented (expected pre-Wave 2)." >&2
      exit 2
    elif [ $py_rc -ne 0 ]; then
      echo "❌ Python schema extraction failed (exit $py_rc)" >&2
      exit $py_rc
    fi

    # Diff against the committed reference (normalized comparison).
    if diff -u \
         <(grep -v '^-- Generated:' "$REFERENCE_FIXTURE") \
         <(grep -v '^-- Generated:' "$tmp_py_schema") \
       > /tmp/schema_diff.txt
    then
      echo "✅ Python schema matches TS reference (zero diff)." >&2
      exit 0
    else
      echo "❌ Schema DRIFT detected:" >&2
      cat /tmp/schema_diff.txt >&2
      exit 1
    fi
    ;;

  --check-reference)
    # Re-extract TS schema to /tmp and diff against committed reference.
    # Useful in CI to catch "someone bumped the LCM pin but forgot to refresh."
    if [ ! -f "$REFERENCE_FIXTURE" ]; then
      echo "❌ Reference fixture missing." >&2
      exit 1
    fi
    tmp_ts_schema="$(mktemp)"
    trap 'rm -f "$tmp_ts_schema"' EXIT
    extract_ts_schema "$tmp_ts_schema"
    if diff -u \
         <(grep -v '^-- Generated:' "$REFERENCE_FIXTURE") \
         <(grep -v '^-- Generated:' "$tmp_ts_schema") \
       > /tmp/ref_check.txt
    then
      echo "✅ Reference is fresh." >&2
      exit 0
    else
      echo "❌ Committed reference is STALE vs current LCM source:" >&2
      cat /tmp/ref_check.txt >&2
      echo "" >&2
      echo "Action: run $0 --refresh-reference and commit." >&2
      exit 1
    fi
    ;;

  *)
    usage
    ;;
esac
