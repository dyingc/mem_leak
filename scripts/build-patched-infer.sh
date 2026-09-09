#!/bin/bash
# Build Infer v1.2.0 with our two Pulse model patches and the argument-transport fix:
#
#   --pulse-model-free-arg-pattern  N:regex   release argument N, not just the first
#   --pulse-model-alloc-arg-pattern N:regex   acquire through a T **out parameter
#   notes/infer-argfile-transport.patch       arguments containing '^' (every anchored regex)
#                                             are forwarded to sub-processes instead of dropped
#
# Neither exists in stock Infer, so `--infer-pattern-mode anchored-argn` needs this build.
# Everything lands under tools/ (gitignored): ~7 GB, ~40 min on 12 cores.
# LLVM/clang is NOT compiled -- the release tarball's prebuilt clang is symlinked in.
#
#   ./scripts/build-patched-infer.sh              # build, skipping steps already done
#   ./scripts/build-patched-infer.sh --verify     # only re-run the verification
#   ./scripts/build-patched-infer.sh --jobs 4
#
# Safe to re-run: each step detects whether it is already done.
set -u -o pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
T=$REPO/tools
INFER_VERSION=v1.2.0
INFER_COMMIT=4c53e80cac9ab1066920593fc1ef4d81e8d2e0c6      # = tag v1.2.0
OPAM_VERSION=2.5.2
SWITCH=4.14.0+flambda
JOBS=$(nproc 2>/dev/null || echo 8)
VERIFY_ONLY=0

while [ $# -gt 0 ]; do
  case $1 in
    --verify) VERIFY_ONLY=1; shift ;;
    --jobs)   JOBS=$2; shift 2 ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say  () { echo; echo "=== $* ($(date +%T))"; }
skip () { echo "    already done, skipping: $*"; }
die  () { echo "ERROR: $*" >&2; exit 1; }

export PATH=$T/local/bin:$T/opam-bin:$PATH
export OPAMROOT=$T/opam-root OPAMYES=1 OPAMJOBS=$JOBS

opam_env () { eval "$(opam env --switch=$SWITCH --set-switch 2>/dev/null)"; }

# --------------------------------------------------------------------------- #
if [ $VERIFY_ONLY -eq 0 ]; then

say "0. system dependencies"
missing=""
for c in gcc g++ make curl git python3 sqlite3 cmake; do
  command -v $c >/dev/null || missing="$missing $c"
done
[ -n "$missing" ] && die "missing tools:$missing
  Debian/Ubuntu: sudo apt-get install -y build-essential curl git python3 sqlite3 cmake \\
      pkg-config libgmp-dev libmpfr-dev libsqlite3-dev zlib1g-dev bubblewrap"
echo "    ok: $(gcc --version | head -1)"

# autoconf >= 2.72 / automake >= 1.17 are needed by ./autogen.sh; build into tools/local
# if the system ones are too old, without touching anything outside the repo.
need_ac=0
command -v autoconf >/dev/null || need_ac=1
if [ $need_ac -eq 0 ]; then
  v=$(autoconf --version | head -1 | grep -oE '[0-9]+\.[0-9]+')
  [ "$(printf '%s\n2.72\n' "$v" | sort -V | head -1)" = "2.72" ] || need_ac=1
fi
if [ $need_ac -eq 1 ]; then
  say "0b. building autoconf/automake into tools/local (system ones too old)"
  mkdir -p "$T/local" "$T/build" && cd "$T/build"
  curl -fsSL https://ftp.gnu.org/gnu/autoconf/autoconf-2.72.tar.gz | tar xz
  (cd autoconf-2.72 && ./configure --prefix="$T/local" >/dev/null && make -s install >/dev/null)
  curl -fsSL https://ftp.gnu.org/gnu/automake/automake-1.17.tar.gz | tar xz
  (cd automake-1.17 && ./configure --prefix="$T/local" >/dev/null && make -s install >/dev/null)
  echo "    $(autoconf --version | head -1)"
else
  skip "autoconf $(autoconf --version | head -1 | grep -oE '[0-9]+\.[0-9]+')"
fi

say "1. Infer $INFER_VERSION release (provides the prebuilt clang, ~1.1 GB)"
mkdir -p "$T"
if [ -x "$T/infer/bin/infer" ]; then
  skip "$T/infer ($("$T/infer/bin/infer" --version | head -1))"
else
  cd "$T"
  curl -fSL --progress-bar -o infer.tar.xz \
    "https://github.com/facebook/infer/releases/download/$INFER_VERSION/infer-linux-x86_64-$INFER_VERSION.tar.xz"
  tar xf infer.tar.xz && mv "infer-linux-x86_64-$INFER_VERSION" infer && rm infer.tar.xz
  "$T/infer/bin/infer" --version | head -1
fi

say "2. Infer source at $INFER_COMMIT"
if [ -d "$T/infer-src/.git" ] && \
   [ "$(cd "$T/infer-src" && git rev-parse HEAD)" = "$INFER_COMMIT" ]; then
  skip "$T/infer-src at the right commit"
else
  [ -d "$T/infer-src" ] && die "$T/infer-src exists but is at the wrong commit; move it away first"
  git clone https://github.com/facebook/infer.git "$T/infer-src"
  (cd "$T/infer-src" && git checkout -q "$INFER_COMMIT")
fi

say "3. symlink the release's clang into the source tree (skips a 3-hour LLVM build)"
FCP=$T/infer-src/facebook-clang-plugins
if [ -L "$FCP/clang/install" ] && [ -L "$FCP/libtooling/build" ]; then
  skip "clang symlinks in place"
else
  cd "$FCP"
  rmdir clang/install libtooling/build 2>/dev/null
  ln -sfn "$T/infer/lib/infer/facebook-clang-plugins/clang/install"    clang/install
  ln -sfn "$T/infer/lib/infer/facebook-clang-plugins/libtooling/build" libtooling/build
  ./clang/setup.sh --only-record-install
  ls -la clang/install libtooling/build
fi

say "4. opam $OPAM_VERSION and the OCaml $SWITCH switch"
if [ ! -x "$T/opam-bin/opam" ]; then
  mkdir -p "$T/opam-bin" && cd "$T/opam-bin"
  curl -fsSL -o opam "https://github.com/ocaml/opam/releases/download/$OPAM_VERSION/opam-$OPAM_VERSION-x86_64-linux"
  chmod +x opam
fi
if [ -d "$OPAMROOT/$SWITCH" ]; then
  skip "switch $SWITCH exists"
else
  opam init --bare --no-setup
  cd "$T/infer-src" && ./build-infer.sh --only-setup-opam -y clang
fi
opam_env
echo "    $(ocamlc -version 2>/dev/null || echo 'ocamlc NOT on PATH')"

say "5. OCaml dependencies"
# The shipped opam/infer.opam.locked no longer solves: cmdliner 1.2.0, conf-gmp 4 and
# mtime 2.0.0 were removed upstream. Install unlocked, then pin the versions that matter.
cd "$T/infer-src"
if ocamlfind query atdgen >/dev/null 2>&1 && ocamlfind query yojson >/dev/null 2>&1; then
  skip "dependencies present (atdgen $(opam list atdgen --short --columns=version 2>/dev/null))"
else
  opam install --deps-only ./opam/infer.opam --assume-depexts

  # atdgen 4.x renamed its findlib library and yojson 3.0 dropped
  # Yojson.Safe.start_any_variant -- both break the build outright.
  URL=https://github.com/ahrefs/atd/archive/refs/tags/2.15.0.tar.gz
  opam pin add -n atd.2.15.0            "$URL"
  opam pin add -n atdgen-runtime.2.15.0 "$URL"
  opam pin add -n atdgen.2.15.0         "$URL"
  opam install atd.2.15.0 atdgen-runtime.2.15.0 atdgen.2.15.0

  # Pin the rest back to the locked versions, one at a time: a single missing version
  # makes a batch install fail as a whole. mtime.2.0.0 is gone upstream, SKIP is fine.
  for p in yojson.2.1.2 ppx_deriving.5.2.1 ppxlib.0.32.0 sedlex.3.2 sqlite3.5.1.0 \
           re.1.11.0 mtime.2.0.0 ctypes.0.22.0 integers.0.7.0 zarith.1.13 base.v0.15.1 \
           spawn.v0.15.1 camlzip.1.11 extlib.1.7.9 iter.1.8 stdcompat.19 \
           ocamlgraph.2.1.0 ppx_blob.0.7.2 cppo.1.6.9 num.1.5 base64.3.5.1; do
    if opam install "$p" >/dev/null 2>&1; then echo "    ok   $p"; else echo "    SKIP $p"; fi
  done
  # files generated by a wrong atdgen version would survive and break the build
  git clean -fdxq infer/src/atd
fi

say "6. apply notes/infer-arg-models.patch and notes/infer-argfile-transport.patch"
cd "$T/infer-src"
# patch 1: Config.ml Config.mli PulseModelsC.ml (arg-position models)
# patch 2: CommandLineOption.ml (arguments containing '^' reach sub-processes intact)
apply_patch () {   # apply_patch <patch> <expected file count> <file regex>
  local n; n=$(git diff --name-only | grep -c -E "$3" || true)
  if [ "$n" -eq "$2" ]; then
    skip "$1 already applied"
  elif [ "$n" -ne 0 ]; then
    die "the source tree is partially modified; inspect 'git -C $T/infer-src diff' yourself"
  else
    git apply --check "$REPO/notes/$1" || die "$1 does not apply -- is the tree at $INFER_COMMIT?"
    git apply "$REPO/notes/$1"
  fi
}
apply_patch infer-arg-models.patch 3 'Config\.mli?$|PulseModelsC\.ml$'
apply_patch infer-argfile-transport.patch 1 'CommandLineOption\.ml$'
git diff --stat

say "7. build (jobs=$JOBS)"
cd "$T/infer-src"
opam_env
[ -f Makefile.autoconf ] || ./autogen.sh
[ -f Makefile.autoconf ] || die "autogen.sh produced no Makefile.autoconf"
if ! grep -q "BUILD_C_ANALYZERS = yes" Makefile.autoconf 2>/dev/null; then
  ./configure --disable-java-analyzers --disable-erlang-analyzers \
              --disable-hack-analyzers --disable-python-analyzers
fi
SECONDS=0
make -j"$JOBS" opt || die "build failed; see the output above"
echo "    built in ${SECONDS}s: $(ls -la infer/bin/infer | awk '{print $5" bytes"}')"

# a ready-made env file so later rebuilds do not need opam on PATH
cat > "$T/infer-env.sh" <<ENVEOF
# source this, then: cd tools/infer-src && make -j8 opt
export OPAMROOT=$T/opam-root
export OPAM_SWITCH_PREFIX=\$OPAMROOT/$SWITCH
export CAML_LD_LIBRARY_PATH=\$OPAM_SWITCH_PREFIX/lib/stublibs:\$OPAM_SWITCH_PREFIX/lib/ocaml/stublibs:\$OPAM_SWITCH_PREFIX/lib/ocaml
export OCAML_TOPLEVEL_PATH=\$OPAM_SWITCH_PREFIX/lib/toplevel
export PATH=\$OPAM_SWITCH_PREFIX/bin:\$PATH
ENVEOF
echo "    wrote $T/infer-env.sh"

fi   # VERIFY_ONLY
# --------------------------------------------------------------------------- #

say "8. verify the patched binary"
"$REPO/results/infer-arg-models/verify_patch.sh" "$T/infer-src/infer/bin/infer" || \
  die "verification failed -- the build is not usable"
"$REPO/results/infer-arg-models/verify_transport.sh" "$T/infer-src/infer/bin/infer" || \
  die "argument-transport verification failed -- anchored regexes would not reach sub-processes"

say "9. verify the stock binary FAILS the same checks"
# Without this the previous step proves little: it must be able to tell the two apart.
if "$REPO/results/infer-arg-models/verify_patch.sh" "$T/infer/bin/infer" >/dev/null 2>&1; then
  die "the stock binary passed the patch checks -- the verifier is not discriminating"
else
  echo "    ok: stock Infer fails the checks, as it must"
fi

cat <<DONE

=== done

  patched Infer : $T/infer-src/infer/bin/infer
  stock Infer   : $T/infer/bin/infer      (control group, keep it)

Use it:

  python -m memhint stage2 --project <p> --out <o> --analyzer infer \\
      --infer-bin $T/infer-src/infer/bin/infer \\
      --infer-pattern-mode anchored-argn

Rebuild after editing the OCaml source:

  source $T/infer-env.sh && cd $T/infer-src && make -j$JOBS opt

Details: notes/infer-arg-models.md
DONE
