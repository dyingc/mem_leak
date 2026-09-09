#!/bin/bash
# Acceptance tests for the INFER_ARGS transport fix (Infer v1.2.0 + notes/infer-argfile-transport.patch).
#
#   ./verify_transport.sh [path-to-infer]      (default: ../../tools/infer-src/infer/bin/infer)
#
# Stock Infer joins the arguments it forwards to its sub-processes with '^' and silently drops any
# argument containing '^' (e.g. every anchored regex), leaving a dangling option behind. The fix
# keeps the forwarded arguments as a list and writes them to the argfile that was already the real
# transport, so nothing is dropped and no separator character is special.
#
# Exit code 0 = every check passed. Each check prints PASS / FAIL and, on failure, what it expected
# versus what it got. Every infer invocation's output is scanned for the caret warning.
set -u
cd "$(dirname "$0")"
INFER=$(readlink -f "${1:-${INFER:-../../tools/infer-src/infer/bin/infer}}")
PASS=0; FAIL=0
ok  () { echo "PASS  $1"; PASS=$((PASS+1)); }
bad () { echo "FAIL  $1"; echo "        $2"; FAIL=$((FAIL+1)); }
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
export TMPDIR=$WORK/tmp; mkdir -p "$TMPDIR"     # infer writes its argfiles here (kept with --debug)
LOGS=$WORK/all-infer-output.log; : > "$LOGS"

# the regex under test exercises every special character the transport must not touch
ALLOC='^\(my_alloc\|other_alloc\)$'
ALLOC_WIDE='^\(my_alloc\|other_alloc\|my_alloc2\)$'   # positive control: the decoy IS matched
FREE1='1:^\(my_free2\)$'
FREE_STOCK='^\(my_free2\)$'
LEAKS='a_leak a_leak_other b_leak b_leak_other'
LEAKS_WIDE='a_decoy a_leak a_leak_other b_decoy b_leak b_leak_other'
LEAKS_STOCK_FREE='a_leak a_leak_other a_ok b_leak b_leak_other b_ok'   # stock free model frees arg 0 (ctx)

# a two-file make project, built fresh for every run
new_project () {   # new_project <dir>
  rm -rf "$1"; mkdir -p "$1"
  cp transport_a.c transport_b.c "$1/"
  printf 'all: transport_a.o transport_b.o\n%%.o: %%.c\n\tgcc -c $< -o $@\n' > "$1/Makefile"
}
# sorted procedure names of the leak reports of a results dir (or "NO-REPORT")
procs_of () {
  python3 - "$1/report.json" <<'PY'
import json, sys
try:
    rs = json.load(open(sys.argv[1]))
except Exception:
    print("NO-REPORT"); sys.exit()
print(*sorted(r["procedure"] for r in rs if r["bug_type"] == "MEMORY_LEAK_C"))
PY
}
# run infer in a fresh project; usage: run_infer <results-dir> <infer args...> (no build command)
run_infer () {
  local out=$1; shift
  new_project "$WORK/proj"
  ( cd "$WORK/proj" && "$INFER" "$@" --results-dir "$out" -- make ) > "$WORK/last.log" 2>&1
  local rc=$?
  cat "$WORK/last.log" >> "$LOGS"
  return $rc
}
expect_procs () {   # expect_procs <label> "<expected procs>" <results-dir>
  local got; got=$(procs_of "$3")
  if [ "$got" = "$2" ]; then ok "$1"; else bad "$1" "expected [$2], got [$got]"; fi
}

echo "=== binary under test: $INFER"
[ -x "$INFER" ] || { echo "FAIL  not executable: $INFER"; exit 1; }
"$INFER" --version | head -1
echo

# ---- 1+2. infer run: capture (make -> compiler-wrapper subprocesses) + analyze (worker children) --
echo "--- infer run, --jobs 2: models must survive both the capture and the analysis subprocesses"
if run_infer "$WORK/run2" run --pulse-only --jobs 2 \
     --pulse-model-alloc-pattern "$ALLOC" --pulse-model-free-arg-pattern "$FREE1"; then
  ok "infer run --jobs 2 exits 0 (capture subprocesses accepted the forwarded arguments)"
else
  bad "infer run --jobs 2 exits 0" "exit code $?; log: $(grep -m1 -i 'needs an argument\|error' "$WORK/last.log")"
fi
if grep -q "Parallel jobs: 2" "$WORK/run2/logs" 2>/dev/null; then ok "analysis really ran in 2 worker processes"
else bad "analysis really ran in 2 worker processes" "'Parallel jobs: 2' not in $WORK/run2/logs"; fi
expect_procs "allocator (\\| alternatives) + arg-1 deallocator active in the workers" "$LEAKS" "$WORK/run2"

echo
echo "--- the anchored regex is not weakened to a prefix match (decoy my_alloc2)"
run_infer "$WORK/wide" run --pulse-only --jobs 2 \
     --pulse-model-alloc-pattern "$ALLOC_WIDE" --pulse-model-free-arg-pattern "$FREE1"
expect_procs "positive control: widening the regex to include my_alloc2 reports the decoys" "$LEAKS_WIDE" "$WORK/wide"
echo "      (so the absence of a_decoy/b_decoy above is due to exact matching, not to a lost model)"
run_infer "$WORK/stock" run --pulse-only --jobs 2 \
     --pulse-model-alloc-pattern "$ALLOC" --pulse-model-free-pattern "$FREE_STOCK"
expect_procs "stock arg-0 free model misfires on the ctx argument (arg-position option is what fixes it)" \
     "$LEAKS_STOCK_FREE" "$WORK/stock"

echo
echo "--- the regex is the last option before '--' (the capture wrapper used to die: option needs an argument)"
if run_infer "$WORK/last" run --pulse-only --jobs 2 --pulse-model-alloc-pattern "$ALLOC"; then
  ok "infer run with the regex as the last option exits 0"
else
  bad "infer run with the regex as the last option exits 0" "exit code $?: $(grep -m1 'needs an argument\|capture command failed' "$WORK/last.log")"
fi
expect_procs "allocator model active (no deallocator model: a_ok/b_ok pass p to an unknown function)" "$LEAKS" "$WORK/last"

echo
echo "--- direct infer analyze: originating process (--jobs 1), forked workers (--jobs 2),"
echo "    exec'd workers (--jobs 2 --no-unix-fork: fresh processes that only see INFER_ARGS)"
new_project "$WORK/cap"
( cd "$WORK/cap" && "$INFER" capture --results-dir "$WORK/cap/out" -- make ) >> "$LOGS" 2>&1
for MODE in "1" "2" "2 --no-unix-fork"; do
  set -- $MODE; J=$1; shift
  D=$WORK/an$J${1:+-exec}
  rm -rf "$D"; cp -r "$WORK/cap/out" "$D"
  "$INFER" analyze --results-dir "$D" --pulse-only --jobs $J "$@" \
      --pulse-model-alloc-pattern "$ALLOC" --pulse-model-free-arg-pattern "$FREE1" >> "$LOGS" 2>&1
  expect_procs "infer analyze --jobs $MODE" "$LEAKS" "$D"
done

# ---- 1. the exact regex reaches every sub-process: inspect the argfiles infer forwards ----------
echo
echo "--- argfiles written by every infer process (kept with --debug, TMPDIR=$TMPDIR)"
rm -f "$TMPDIR"/args*
run_infer "$WORK/dbg" run --debug --pulse-only --jobs 2 --no-unix-fork \
     --pulse-model-alloc-pattern "$ALLOC" --pulse-model-free-arg-pattern "$FREE1"
python3 - "$TMPDIR" "$ALLOC" "$FREE1" <<'PY' && ok "every process's forwarded arguments carry both regexes verbatim (originator, capture wrappers, exec'd analysis workers)" \
  || bad "every process's forwarded arguments carry both regexes verbatim" "see the lines above"
import glob, os, sys
tmp, alloc, free1 = sys.argv[1:]
# sub-processes re-export their own argfile; it references the parent's one as "@<path>" exactly like
# infer's own argfile parser sees it, so expand those references the same way before checking.
def expand(path, seen=()):
    out = []
    for line in open(path).read().split("\n"):
        if line.startswith("@") and os.path.isfile(line[1:]) and line[1:] not in seen:
            out += expand(line[1:], seen + (path,))
        else:
            out.append(line)
    return out
files = sorted(glob.glob(os.path.join(tmp, "args*")))
n_ok = n_child = n_with = 0
for f in files:
    lines = expand(f)
    if "--pulse-model-alloc-pattern" not in lines: continue    # e.g. helper `infer --version` runs
    n_with += 1
    a = lines[lines.index("--pulse-model-alloc-pattern") + 1]
    fr = lines[lines.index("--pulse-model-free-arg-pattern") + 1]
    child = "--run-as-child" in lines
    n_child += child
    good = a == alloc and fr == free1
    n_ok += good
    kind = "analysis worker " if child else "originator/capture"
    print(f"      {kind}  {os.path.basename(f)}  alloc={a!r}  free={fr!r}  {'OK' if good else 'MISMATCH'}")
print(f"      {len(files)} argfiles, {n_with} carry the model options, {n_ok} intact, {n_child} written by exec'd analysis workers")
sys.exit(0 if n_ok == n_with and n_with >= 3 and n_child >= 1 else 1)
PY

# ---- 5. the user-facing INFER_ARGS env var (^-separated) still works -------------------------
echo
echo "--- INFER_ARGS environment variable"
new_project "$WORK/proj"
( cd "$WORK/proj" && INFER_ARGS='--pulse-only^--jobs^2' "$INFER" run --results-dir "$WORK/env" \
      --pulse-model-alloc-pattern "$ALLOC" --pulse-model-free-arg-pattern "$FREE1" -- make ) > "$WORK/last.log" 2>&1
cat "$WORK/last.log" >> "$LOGS"
if grep -q "Parallel jobs: 2" "$WORK/env/logs" 2>/dev/null; then ok "ordinary ^-separated INFER_ARGS options are honoured (--jobs 2 taken from the env)"
else bad "ordinary ^-separated INFER_ARGS options are honoured" "'Parallel jobs: 2' not in $WORK/env/logs"; fi
expect_procs "command-line models combine with INFER_ARGS options" "$LEAKS" "$WORK/env"

# ---- 7. compatibility: explicit @argfile and .inferconfig -----------------------------------
echo
echo "--- compatibility: explicit @argfile and .inferconfig"
printf -- '--pulse-only\n--jobs\n2\n--pulse-model-alloc-pattern\n%s\n--pulse-model-free-arg-pattern\n%s\n' "$ALLOC" "$FREE1" > "$WORK/opts.txt"
new_project "$WORK/proj"
( cd "$WORK/proj" && "$INFER" run "@$WORK/opts.txt" --results-dir "$WORK/argfile" -- make ) > "$WORK/last.log" 2>&1
cat "$WORK/last.log" >> "$LOGS"
expect_procs "explicit @argfile on the command line" "$LEAKS" "$WORK/argfile"
new_project "$WORK/proj"
( cd "$WORK/proj" && INFER_ARGS="@$WORK/opts.txt" "$INFER" run --results-dir "$WORK/envfile" -- make ) > "$WORK/last.log" 2>&1
cat "$WORK/last.log" >> "$LOGS"
expect_procs "explicit @argfile through INFER_ARGS" "$LEAKS" "$WORK/envfile"
new_project "$WORK/proj"
cat > "$WORK/proj/.inferconfig" <<JSON
{ "pulse-only": true, "jobs": 2,
  "pulse-model-alloc-pattern": "^\\\\(my_alloc\\\\|other_alloc\\\\)$",
  "pulse-model-free-arg-pattern": ["1:^\\\\(my_free2\\\\)$"] }
JSON
( cd "$WORK/proj" && "$INFER" run --results-dir "$WORK/cfg" -- make ) > "$WORK/last.log" 2>&1
cat "$WORK/last.log" >> "$LOGS"
expect_procs ".inferconfig in the project root" "$LEAKS" "$WORK/cfg"

# ---- 6. no caret warning, nothing dropped, in any of the runs above --------------------------
echo
n=$(grep -c "Ignoring unsupported option containing" "$LOGS")
if [ "$n" -eq 0 ]; then ok "no 'Ignoring unsupported option containing ^' warning in any run"
else bad "no caret warning in any run" "$n warning line(s), e.g.: $(grep -m1 'Ignoring unsupported' "$LOGS")"; fi

echo
echo "=== $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
