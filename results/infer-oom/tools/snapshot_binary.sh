#!/bin/bash
# snapshot_binary.sh <name> : copy the freshly built infer binary under output/oom/tools/<name>/
set -e
N=$1; T=/home/edong/VSCode/papers/mem_leak/output/oom/tools/$N
mkdir -p "$T/bin"
cp /home/edong/VSCode/papers/mem_leak/tools/infer-src/infer/bin/infer "$T/bin/infer"
ln -sfn /home/edong/VSCode/papers/mem_leak/tools/infer-src/infer/lib "$T/lib"
"$T/bin/infer" --version | head -1
