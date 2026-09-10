#!/bin/bash
# snapshot_binary.sh <name> [source-tree] : copy the freshly built infer binary under
# output/oom/tools/<name>/ so it survives the next rebuild of the source tree.
# source-tree defaults to the v1.2.0 tree; pass tools/infer-src-1.3 for the v1.3.0 one.
set -e
N=$1; SRC=${2:-/home/edong/VSCode/papers/mem_leak/tools/infer-src}
T=/home/edong/VSCode/papers/mem_leak/output/oom/tools/$N
mkdir -p "$T/bin"
cp "$SRC/infer/bin/infer" "$T/bin/infer"
ln -sfn "$SRC/infer/lib" "$T/lib"
"$T/bin/infer" --version | head -1
