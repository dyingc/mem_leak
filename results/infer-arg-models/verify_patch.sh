#!/bin/bash
# Verify that an Infer binary carries both of our Pulse model patches.
#
#   ./verify_patch.sh [path-to-infer]      (default: ../../tools/infer-src/infer/bin/infer)
#
# Exit code 0 = every check passed. Each check prints PASS / FAIL and, on failure,
# what it expected versus what it got.
set -u
cd "$(dirname "$0")"
INFER=${1:-${INFER:-../../tools/infer-src/infer/bin/infer}}
PASS=0; FAIL=0
ok   () { echo "PASS  $1"; PASS=$((PASS+1)); }
bad  () { echo "FAIL  $1"; echo "        $2"; FAIL=$((FAIL+1)); }
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT

# count leak/uaf reports of a run; prints "<n> <bugtype:proc> ..."
run_count () {   # run_count <source.c> <flags...>
  local src=$1; shift
  rm -rf "$WORK/out"
  "$INFER" run --results-dir "$WORK/out" --pulse-only "$@" -- \
      gcc -c "$src" -o /dev/null > "$WORK/log" 2>&1
  python3 - "$WORK/out/report.json" <<'PY'
import json, sys
try:
    rs = json.load(open(sys.argv[1]))
except Exception:
    rs = []
print(len(rs), *sorted(f'{r["bug_type"]}:{r["procedure"]}' for r in rs))
PY
}
expect () {  # expect <label> <expected-count> <source> <flags...>
  local label=$1 want=$2 src=$3; shift 3
  local got; got=$(run_count "$src" "$@")
  local n=${got%% *}
  if [ "$n" = "$want" ]; then ok "$label ($n report(s))"
  else bad "$label" "expected $want report(s), got: $got"; fi
}

echo "=== binary under test: $INFER"
[ -x "$INFER" ] || { echo "FAIL  not executable: $INFER"; exit 1; }
"$INFER" --version | head -1
echo

# ---- 1. the options exist at all -----------------------------------------
H=$("$INFER" analyze --help 2>&1)
for o in pulse-model-free-arg-pattern pulse-model-alloc-arg-pattern; do
  if grep -q -- "--$o" <<<"$H"; then ok "option --$o is present"
  else bad "option --$o is present" "not in 'infer analyze --help' -- this binary is not patched"; fi
done
if [ "$FAIL" -ne 0 ]; then
  # Without the options every later check would "pass" vacuously: infer exits with
  # 'unknown option' and writes no report, so a 0-report expectation is met for the
  # wrong reason. Stop here instead of printing a misleading tally.
  echo
  echo "=== $PASS passed, $FAIL failed -- the options are missing, so the behaviour checks"
  echo "    were skipped (they would pass vacuously on an empty report)."
  exit 1
fi
echo

# ---- 2. malformed N:regex is rejected, not silently ignored ---------------
if "$INFER" analyze --results-dir "$WORK/x" --pulse-model-alloc-arg-pattern 'nope' 2>&1 \
     | grep -q "expects N:regex"; then ok "malformed N:regex is rejected"
else bad "malformed N:regex is rejected" "expected a UserError mentioning 'expects N:regex'"; fi
echo

# ---- 3. patch 1: free at argument positions 1 and 2 ----------------------
A='--pulse-model-alloc-pattern'; AR='^\(my_alloc\)$'
echo "--- patch 1: --pulse-model-free-arg-pattern (argn.c)"
expect "only leak_none is reported (allocator model alone)" 1 argn.c $A "$AR"
# stock free model releases arg0, so it frees the ctx: 4 leaks + 1 use-after-free
expect "native free model on a 2nd-arg API misfires" 5 argn.c \
    $A "$AR" --pulse-model-free-pattern '^\(my_free2\|my_free3\)$'
expect "arg-position free model fixes it" 1 argn.c \
    $A "$AR" --pulse-model-free-arg-pattern '1:^\(my_free2\)$' \
             --pulse-model-free-arg-pattern '2:^\(my_free3\)$'
echo

# ---- 4. patch 2: acquisition through an out parameter --------------------
echo "--- patch 2: --pulse-model-alloc-arg-pattern (out_arg.c)"
OUT0='0:^\(make_a\|make_c\|make_two\)$'; OUT1='1:^\(make_b\|make_maybe\)$'
F0='0:^\(destroy_resource\|destroy_s\)$'; F1='1:^\(destroy_ctx1\)$'; F2='2:^\(destroy_ctx2\)$'
expect "no models -> nothing found (all callees are extern)" 0 out_arg.c
expect "return-value acquisition still works"                1 out_arg.c $A '^\(make_resource\)$'
expect "out-parameter acquisition finds 5 leaks"             5 out_arg.c \
    --pulse-model-alloc-arg-pattern "$OUT0" --pulse-model-alloc-arg-pattern "$OUT1"
expect "with releases configured, only the 6 real leaks remain" 6 out_arg.c \
    $A '^\(make_resource\)$' \
    --pulse-model-alloc-arg-pattern "$OUT0" --pulse-model-alloc-arg-pattern "$OUT1" \
    --pulse-model-free-arg-pattern "$F0" --pulse-model-free-arg-pattern "$F1" \
    --pulse-model-free-arg-pattern "$F2"
expect "a scalar at the configured index acquires nothing"   0 out_arg.c \
    --pulse-model-alloc-arg-pattern '0:^\(make_b\|ordinary_function\|fill_int_out\)$' \
    --pulse-model-free-arg-pattern "$F0"
expect "a void* at the configured index acquires nothing"    0 out_arg.c \
    --pulse-model-alloc-arg-pattern '1:^\(ordinary_function\)$'
echo

echo "=== $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
