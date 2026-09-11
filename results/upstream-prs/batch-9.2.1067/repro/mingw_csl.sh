#!/bin/bash
# Build vim 9.2.1067 for MS-Windows with buffer_csl.diff applied, using the
# mingw-w64 cross compiler, then exercise the patched path under Wine.
# This is the BACKSLASH_IN_FILENAME build that cannot be produced on Linux natively.
#
# Needs: x86_64-w64-mingw32-gcc, x86_64-w64-mingw32-windres, wine.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
VIMREPO=${VIMREPO:-/home/edong/VSCode/papers/mem_leak/subjects/vim_master}
REV=${REV:-90fdb790}
WORK=${WORK:-/tmp/mingw_csl_$$}

git -C "$VIMREPO" worktree add --detach "$WORK" "$REV" >/dev/null 2>&1 || exit 1
trap 'git -C "$VIMREPO" worktree remove --force "$WORK" >/dev/null 2>&1' EXIT

git -C "$WORK" apply "$HERE/../buffer_csl.diff" || { echo "patch did not apply"; exit 1; }
cd "$WORK/src" || exit 1

echo -n "BACKSLASH_IN_FILENAME defined in this build: "
x86_64-w64-mingw32-gcc -E -dM -I. -Iproto -DWIN32 -DWINVER=0x0601 -DFEAT_HUGE \
    -DHAVE_PATHDEF vim.h 2>/dev/null | grep -q "define BACKSLASH_IN_FILENAME" \
    && echo yes || { echo no; exit 1; }

make -f Make_ming.mak CROSS=yes CROSS_COMPILE=x86_64-w64-mingw32- \
     WINDRES=x86_64-w64-mingw32-windres ARCH=x86-64 FEATURES=HUGE \
     GUI=no OLE=no IME=no DEBUG=no -j"$(nproc)" > build.log 2>&1 \
    || { echo "build failed"; tail -20 build.log; exit 1; }
echo "vim.exe built, no warnings in buffer.c: $(grep -ci 'buffer\.c.*\(warning\|error\)' build.log) hit(s)"

# 1. the empty_option path, 2. buffer free, 3. buf_copy_options() re-entry
cat > csl.vim <<'EOF'
set completeslash=slash
new
setlocal completeslash=backslash
set completeslash=
bwipe!
for i in range(50)
  new
  setlocal completeslash=slash
  bwipe!
endfor
set cpo+=S
set completeslash=slash
new
file Xone
new
file Xtwo
for i in range(200)
  buffer Xone
  buffer Xtwo
endfor
set completeslash=
for i in range(200)
  buffer Xone
  buffer Xtwo
endfor
call writefile(['survived'], 'Xcslok.txt')
qa!
EOF
rm -f Xcslok.txt
WINEDEBUG=-all timeout 300 wine ./vim.exe -u NONE -i NONE -N --not-a-term -S csl.vim >/dev/null 2>&1
rc=$?
if [ -f Xcslok.txt ] && [ $rc = 0 ]; then
    echo "Wine run: 50 buffer create/wipe cycles + 400 buf_copy_options() re-entries, no crash"
    exit 0
fi
echo "Wine run FAILED (exit $rc)"
exit 1
