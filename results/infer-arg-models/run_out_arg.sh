#!/bin/bash
# Reproduce the out-parameter-acquisition test matrix.
set -u
cd "$(dirname "$0")"
INFER=${INFER:-/home/edong/VSCode/papers/mem_leak/tools/infer-src/infer/bin/infer}

ALLOC_RET='^\(make_resource\)$'
FREE0='0:^\(destroy_resource\|destroy_s\)$'
FREE1='1:^\(destroy_ctx1\)$'
FREE2='2:^\(destroy_ctx2\)$'
ALLOC_OUT0='0:^\(make_a\|make_c\|make_two\)$'
ALLOC_OUT1='1:^\(make_b\|make_maybe\)$'

run () {   # run <name> <extra flags...>
  local name=$1; shift
  rm -rf "infer-$name"
  $INFER run --results-dir "infer-$name" --pulse-only "$@" -- \
      gcc -c out_arg.c -o /dev/null > /dev/null 2>&1
  echo "### $name"
  if [ -s "infer-$name/report.txt" ]; then
    grep -oP '^out_arg\.c:\d+: error: \w+\s*\n?' "infer-$name/report.txt" > /dev/null
    grep -E '^out_arg\.c:[0-9]+: (error|warning)' "infer-$name/report.txt" \
      | sed 's/, [0-9]*, [A-Z_]*, .*//'
  fi
  python3 - "infer-$name/report.json" <<'PY'
import json, sys
try:
    rs = json.load(open(sys.argv[1]))
except Exception:
    rs = []
for r in sorted(rs, key=lambda r: (r["procedure"], r["line"])):
    print(f'  {r["procedure"]:28s} {r["bug_type"]}')
print(f'  -- {len(rs)} report(s)')
PY
  echo
}

# 1. baseline: no models at all
run baseline

# 2. return-value acquisition only (pre-existing feature)
run alloc-ret --pulse-model-alloc-pattern "$ALLOC_RET"

# 3. free at arbitrary argument positions only (the earlier patch)
run free-argn \
  --pulse-model-free-arg-pattern "$FREE0" \
  --pulse-model-free-arg-pattern "$FREE1" \
  --pulse-model-free-arg-pattern "$FREE2"

# 4. out-parameter acquisition only (the new feature)
run alloc-outarg \
  --pulse-model-alloc-arg-pattern "$ALLOC_OUT0" \
  --pulse-model-alloc-arg-pattern "$ALLOC_OUT1"

# 5. everything together
run full \
  --pulse-model-alloc-pattern "$ALLOC_RET" \
  --pulse-model-alloc-arg-pattern "$ALLOC_OUT0" \
  --pulse-model-alloc-arg-pattern "$ALLOC_OUT1" \
  --pulse-model-free-arg-pattern "$FREE0" \
  --pulse-model-free-arg-pattern "$FREE1" \
  --pulse-model-free-arg-pattern "$FREE2"

# 6. wrong index: configure the acquisition on argument 0 of make_b (an int)
run wrong-index \
  --pulse-model-alloc-arg-pattern '0:^\(make_b\|ordinary_function\|fill_int_out\)$' \
  --pulse-model-free-arg-pattern "$FREE0"

# 7. the configured argument is a plain pointer (void *), not a pointer to a pointer
run plain-pointer \
  --pulse-model-alloc-arg-pattern '1:^\(ordinary_function\)$'

# 8. misconfiguration: the same function appears in an alloc rule and a free rule.
#    Infer gives a procedure at most one model and the release matchers are tried first,
#    so the acquisition rule is silently ignored and the calls become use-after-free.
run rule-collision \
  --pulse-model-alloc-arg-pattern '0:^\(make_a\)$' \
  --pulse-model-free-arg-pattern '0:^\(make_a\|destroy_resource\)$'
