#!/bin/bash
# Build vim 9.2.1067 in a throwaway worktree and run the tests that cover the
# two Linux-buildable patches, in both directions:
#
#   pass 1  match_test.diff only          -> Test_setmatches_pos_refcount must FAIL (3 asserts)
#   pass 2  + match.diff + list_free.diff -> everything must pass
#
# Needs: gcc, make, a pty (uses script(1)).  Takes ~4 min.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
VIMREPO=${VIMREPO:-/home/edong/VSCode/papers/mem_leak/subjects/vim_master}
REV=${REV:-90fdb790}
WORK=${WORK:-/tmp/vimtests_$$}
TESTS="test_match.res test_blob.res test_listdict.res test_tuple.res \
       test_functions.res test_vim9_builtin.res test_vim9_script.res test_expr.res"

git -C "$VIMREPO" worktree add --detach "$WORK" "$REV" >/dev/null 2>&1 || exit 1
trap 'git -C "$VIMREPO" worktree remove --force "$WORK" >/dev/null 2>&1' EXIT

cd "$WORK/src" || exit 1
./configure --with-features=normal --disable-gui --without-x --disable-netbeans >/dev/null 2>&1 \
    || { echo "configure failed"; exit 1; }

run_pass () {   # $1 = label, $2 = expected failure count
    make -j"$(nproc)" >/dev/null 2>&1 || { echo "build failed"; exit 1; }
    cd testdir; rm -f ./*.res messages
    script -qec "make $TESTS" /dev/null >/dev/null 2>&1
    local n ex
    n=$(grep -c "Expected 1 but got 2" messages 2>/dev/null || true)
    ex=$(grep -hoE "Executed [0-9]+ tests" messages | awk '{s+=$2} END{print s+0}')
    echo "$1: $ex tests executed, $n failing assertion(s)"
    cd ..
    [ "$n" = "$2" ] || { echo ">>> UNEXPECTED: wanted $2"; return 1; }
}

git -C "$WORK" apply "$HERE/../match_test.diff" || exit 1
run_pass "pass 1 (test only, fix absent)" 3 || exit 1

git -C "$WORK" apply "$HERE/../match.diff" "$HERE/../list_free.diff" || exit 1
run_pass "pass 2 (test + both fixes)" 0 || exit 1

echo "both directions as expected"
