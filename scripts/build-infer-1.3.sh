#!/bin/bash
# Build Infer v1.3.0 (the current upstream release) next to the v1.2.0 tree.
#
# The v1.2.0 build (scripts/build-patched-infer.sh) is the one the paper reproduction and every
# published measurement used; it stays untouched.  This script builds a SECOND, independent
# toolchain so the five patches can be rebased onto upstream and re-measured without putting the
# working one at risk:
#
#   tools/opam-root/5.3.0+flambda   OCaml 5 switch (v1.3.0 needs it; v1.2.0 needs 4.14)
#   tools/infer-1.3/                v1.3.0 release tarball -- only for its prebuilt clang 21.1.6
#   tools/infer-src-1.3/            v1.3.0 source, built here
#   tools/infer-env-1.3.sh          env file for incremental rebuilds
#
# Patches are NOT applied by this script: they are ported one at a time (see
# notes/infer-1.3-migration.md).  Pass --patched once notes/1.3/*.patch exist.
#
#   ./scripts/build-infer-1.3.sh              # stock v1.3.0
#   ./scripts/build-infer-1.3.sh --jobs 4
#
# Safe to re-run: each step detects whether it is already done.
set -u -o pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
T=$REPO/tools
INFER_VERSION=v1.3.0
INFER_COMMIT=410a6d939                     # = tag v1.3.0
SWITCH=5.3.0+flambda
SWITCH_PKGS="--package=ocaml-variants.5.3.0+options,ocaml-option-flambda"
SRC=$T/infer-src-1.3
REL=$T/infer-1.3
JOBS=$(nproc 2>/dev/null || echo 8)

while [ $# -gt 0 ]; do
  case $1 in
    --jobs)   JOBS=$2; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say  () { echo; echo "=== $* ($(date +%T))"; }
skip () { echo "    already done, skipping: $*"; }
die  () { echo "ERROR: $*" >&2; exit 1; }

export PATH=$T/local/bin:$T/opam-bin:$PATH
export OPAMROOT=$T/opam-root OPAMYES=1 OPAMJOBS=$JOBS
# opam decides depexts from the system package manager, not from PATH: it wants a dpkg-installed
# autoconf (and default-jdk, which we do not build against anyway).  tools/local/bin has a
# self-built autoconf 2.72, so tell opam to take the depexts on trust instead of running apt-get.
export OPAMASSUMEDEPEXTS=1
opam_env () { eval "$(opam env --switch=$SWITCH --set-switch 2>/dev/null)"; }

say "1. $INFER_VERSION release tarball (prebuilt clang, ~1.4 GB unpacked)"
if [ -x "$REL/bin/infer" ]; then
  skip "$REL ($("$REL/bin/infer" --version | head -1))"
else
  cd "$T"
  curl -fSL --progress-bar -o infer-1.3.tar.xz \
    "https://github.com/facebook/infer/releases/download/$INFER_VERSION/infer-linux-x86_64-$INFER_VERSION.tar.xz"
  tar xf infer-1.3.tar.xz && mv "infer-linux-x86_64-$INFER_VERSION" "$REL" && rm infer-1.3.tar.xz
fi

say "2. source at $INFER_VERSION"
if [ -d "$SRC/.git" ]; then
  skip "$SRC at $(git -C "$SRC" rev-parse --short HEAD)"
else
  # clone from the existing v1.2.0 checkout if it is there (no second network fetch)
  if [ -d "$T/infer-src/.git" ]; then git clone -q "$T/infer-src" "$SRC"
  else git clone -q https://github.com/facebook/infer.git "$SRC"; fi
  git -C "$SRC" checkout -q "$INFER_COMMIT"
fi

say "3. symlink the release's clang into the source tree (skips a 3-hour LLVM build)"
FCP=$SRC/facebook-clang-plugins
if [ -L "$FCP/clang/install" ] && [ -L "$FCP/libtooling/build" ]; then
  skip "clang symlinks in place ($("$FCP/clang/install/bin/clang" --version | head -1))"
else
  cd "$FCP"
  rmdir clang/install libtooling/build 2>/dev/null
  ln -sfn "$REL/lib/infer/facebook-clang-plugins/clang/install"    clang/install
  ln -sfn "$REL/lib/infer/facebook-clang-plugins/libtooling/build" libtooling/build
  ./clang/setup.sh --only-record-install
fi

say "4. OCaml $SWITCH switch"
if [ -x "$OPAMROOT/$SWITCH/bin/ocamlc" ]; then
  skip "switch $SWITCH exists"
else
  [ -d "$OPAMROOT/repo" ] || opam init --bare --no-setup
  opam switch create "$SWITCH" $SWITCH_PKGS
fi
opam_env
echo "    $(ocamlc -version)"

say "5. OCaml dependencies"
# Unlike v1.2.0, do NOT run `opam install --deps-only opam/infer.opam*` by hand: v1.3.0 depends on
# packages that are only satisfiable through upstream's own pins --
#   charon 0.1 / name_matcher_parser  vendored in dependencies/charon (not on opam-repository at all;
#                                     needed even with --disable-rust-analyzers, because
#                                     infer/src/integration/dune.in lists `charon` unconditionally)
#   camlzip                           the lock asks for 1.12, which has been withdrawn from
#                                     opam-repository; upstream pins its own fork in dependencies/
#   ppx_show, pyml                    likewise vendored
# `build-infer.sh --only-setup-opam` does exactly those pins and then the locked install.
cd "$SRC"
if ocamlfind query atdgen >/dev/null 2>&1 && ocamlfind query charon >/dev/null 2>&1; then
  skip "dependencies present (atdgen $(opam list atdgen --short --columns=version 2>/dev/null))"
else
  ./build-infer.sh --only-setup-opam --user-opam-switch -y clang \
    || die "opam setup failed; see the output above"
fi

say "6. build (jobs=$JOBS)"
cd "$SRC"
# autogen.sh writes ./configure; ./configure writes Makefile.autoconf
[ -f configure ] || ./autogen.sh
[ -f configure ] || die "autogen.sh produced no ./configure"
if ! grep -q "BUILD_C_ANALYZERS = yes" Makefile.autoconf 2>/dev/null; then
  # rust and swift are off by default in v1.3.0's configure.ac, but say so explicitly
  ./configure --disable-java-analyzers --disable-erlang-analyzers \
              --disable-hack-analyzers --disable-python-analyzers \
              --disable-rust-analyzers --disable-swift-analyzers \
    || die "configure failed"
fi
[ -f Makefile.autoconf ] || die "configure produced no Makefile.autoconf"
SECONDS=0
make -j"$JOBS" opt || die "build failed; see the output above"
echo "    built in ${SECONDS}s: $(ls -la infer/bin/infer | awk '{print $5" bytes"}')"

cat > "$T/infer-env-1.3.sh" <<ENVEOF
# source this, then: cd tools/infer-src-1.3 && make -j8 opt
export OPAMROOT=$T/opam-root
export OPAM_SWITCH_PREFIX=\$OPAMROOT/$SWITCH
export CAML_LD_LIBRARY_PATH=\$OPAM_SWITCH_PREFIX/lib/stublibs:\$OPAM_SWITCH_PREFIX/lib/ocaml/stublibs:\$OPAM_SWITCH_PREFIX/lib/ocaml
export OCAML_TOPLEVEL_PATH=\$OPAM_SWITCH_PREFIX/lib/toplevel
export PATH=$T/opam-bin:\$OPAM_SWITCH_PREFIX/bin:\$PATH
ENVEOF
echo "    wrote $T/infer-env-1.3.sh"

say "7. smoke test"
"$SRC/infer/bin/infer" --version | head -1
cat <<DONE

=== done

  v1.3.0 (stock)  : $SRC/infer/bin/infer
  v1.2.0 (patched): $T/infer-src/infer/bin/infer     <- still the reference build

Rebuild after editing the OCaml source:

  source $T/infer-env-1.3.sh && cd $SRC && make -j$JOBS opt
DONE
