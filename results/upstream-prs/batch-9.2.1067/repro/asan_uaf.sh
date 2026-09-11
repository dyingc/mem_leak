#!/bin/bash
# Reproduce the use-after-free of PR 1 (list_free.diff) on vim 9.2.1067.
#
#   ./asan_uaf.sh              -> pristine 9.2.1067, expect an ASan report
#   ./asan_uaf.sh --patched    -> with list_free.diff applied, expect a clean run
#
# Needs: gcc with -fsanitize=address, gdb.  Build takes ~2 min.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
VIMREPO=${VIMREPO:-/home/edong/VSCode/papers/mem_leak/subjects/vim_master}
REV=${REV:-90fdb790}
WORK=${WORK:-/tmp/asan_uaf_$$}
PATCHED=0
[ "${1:-}" = "--patched" ] && PATCHED=1

git -C "$VIMREPO" worktree add --detach "$WORK" "$REV" >/dev/null 2>&1 || exit 1
trap 'git -C "$VIMREPO" worktree remove --force "$WORK" >/dev/null 2>&1' EXIT

[ $PATCHED = 1 ] && git -C "$WORK" apply "$HERE/../list_free.diff"

cd "$WORK/src" || exit 1
./configure --with-features=normal --disable-gui --without-x --disable-netbeans \
    CFLAGS="-fsanitize=address -fno-omit-frame-pointer -g -O0" \
    LDFLAGS="-fsanitize=address" >/dev/null 2>&1 || { echo "configure failed"; exit 1; }
make -j"$(nproc)" >/dev/null 2>&1 || { echo "build failed"; exit 1; }

# items() on a Blob: list_alloc() at blob.c:328, then list_append_list().
# Force the listitem_alloc() inside list_append_list() to return NULL once,
# so blob2items() takes the vim_free(l2) branch at blob.c:334.
# Then allocate another list, which makes list_init() write through first_list.
cat > uaf.vim <<'EOF'
let b = 0z0011
let r = items(b)
let z = [1, 2, 3]
call garbagecollect()
qa!
EOF
cat > uaf.gdb <<EOF
set confirm off
set pagination off
break blob2items
run -u NONE -i NONE -N --not-a-term -S $WORK/src/uaf.vim
delete 1
break listitem_alloc
continue
return (listitem_T *)0
delete
continue
quit
EOF
ASAN_OPTIONS=detect_leaks=0 gdb -q -batch -x uaf.gdb ./vim 2>&1 | \
    sed -n '/ERROR: AddressSanitizer/,/^SUMMARY/p' | grep -vE '^ *#[0-9]+ .*(eval[0-9]|do_one_cmd|do_cmdline|do_source|call_func|get_func_tv|eval_func|main |__libc)' > asan_report.txt

if [ -s asan_report.txt ]; then
    echo "=== AddressSanitizer reported (bug is present) ==="
    cat asan_report.txt
    [ $PATCHED = 1 ] && echo ">>> UNEXPECTED: the patch should have made this clean" && exit 1
    exit 0
else
    echo "=== no AddressSanitizer error ==="
    [ $PATCHED = 0 ] && echo ">>> UNEXPECTED: pristine 9.2.1067 should report a UAF" && exit 1
    exit 0
fi
