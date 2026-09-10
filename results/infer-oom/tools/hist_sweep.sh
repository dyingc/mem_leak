#!/bin/bash
# hist_sweep.sh <infer-binary> <tag> <shape> <n-list...>
set -u
INFER_BIN=$1; TAG=$2; SHAPE=$3; shift 3
ROOT=/home/edong/VSCode/papers/mem_leak
BUILD_INFER=$ROOT/tools/infer-src/infer/bin/infer
for n in "$@"; do
  D=$ROOT/output/oom/fixtures/hist/$SHAPE$n
  rm -rf "$D"; mkdir -p "$D"
  python3 $ROOT/results/infer-oom/tools/gen_history.py --shape "$SHAPE" --n "$n" --out "$D/f.c" >/dev/null
  ( cd "$D" && nice -n 10 "$BUILD_INFER" capture --results-dir cap -- gcc -c f.c -o f.o >/dev/null 2>&1 )
  INFER="$INFER_BIN" VMEM_KB=${VMEM_KB:-6291456} WALL=${WALL:-420} \
    $ROOT/output/oom/tools/run_guarded.sh "$TAG-$SHAPE$n" "$D/cap" --timeout 600 >/dev/null 2>&1
  S=$ROOT/output/oom/runs/$TAG-$SHAPE$n/summary.txt
  printf '%-8s n=%-3s %s\n' "$SHAPE" "$n" "$(grep -h 'exit=' $S) | $(grep -h 'report.json' $S)"
done
