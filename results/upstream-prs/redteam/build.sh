#!/bin/bash
# usage: build.sh <tree> <variant: orig|fixed> <outname>
set -e
S=/tmp/claude-1000/-home-edong-VSCode-papers-mem-leak/950b3496-e4ae-4405-b846-13a86ca78718/scratchpad
P=/home/edong/VSCode/papers/mem_leak/results/upstream-prs/tip-9.2.1054
T=$S/$1; V=$2; OUT=$3
cd $T && git checkout -q -- src && git status --short | grep -v '^??' || true
if [ "$V" = fixed ]; then for d in $P/*.diff; do git apply "$d"; done; fi
python3 $S/instrument.py $T/src
git diff --stat | tail -1
cd src && make -j6 > $S/logs/make-$OUT.log 2>&1 && cp vim $S/bin/$OUT && echo "BUILD OK $OUT" || { echo "BUILD FAIL $OUT"; tail -20 $S/logs/make-$OUT.log; }
