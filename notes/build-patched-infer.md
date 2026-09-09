# 构建打了补丁的 Infer

**一句话：跑 `./scripts/build-patched-infer.sh` 就行**，它把下面每一步都做了，
每一步都会检测是否已完成，可以反复跑；最后会自动做正反两向的验证。

```bash
cd <repo> && ./scripts/build-patched-infer.sh          # 从零到可用，约 40 分钟
./scripts/build-patched-infer.sh --verify              # 只重跑验证
./scripts/build-patched-infer.sh --jobs 4              # 限制并行度
```

本文档是这个脚本每一步在做什么、以及**为什么这么做**的说明。手工执行时
把 `REPO` 设成仓库根目录即可（脚本自己会推断）。

## 0. 为什么必须重新编译

两个补丁改的是 Infer 的 OCaml 源码，不是配置文件或插件，所以**必须重新编译 Infer 本体**：

| 选项 | 作用 | 官方 Infer |
|---|---|---|
| `--pulse-model-free-arg-pattern N:regex` | 把匹配函数建模为释放第 N 个参数（原生只能释放第一个） | ❌ 没有 |
| `--pulse-model-alloc-arg-pattern N:regex` | 把匹配函数建模为通过 `T **out` 出参交出新分配的内存（原生只能标记返回值） | ❌ 没有 |

补丁在仓库里：`notes/infer-arg-models.patch`（211 行 diff，3 个文件）和 `notes/infer-argfile-transport.patch`（74 行 diff，1 个文件；让含 `^` 的参数——也就是所有锚定正则——能原样传给 Infer 的子进程，见 `notes/infer-argfile-transport.md`）。
`tools/` 在 `.gitignore` 里，所以源码树、opam switch、编译产物**都不在仓库里**，新 VM 上要从头做一遍。

**好消息：不用编译 LLVM/clang。** Infer 的发行版 tarball 里已经带了编译好的 clang（909 MB）
和 AST 插件，把它们软链接进源码树就行。全程只编译 OCaml 部分。

预算：**磁盘约 7 GB，时间约 40 分钟**（12 核 / 28 GB 的机器上实测）。

---

## 1. 系统依赖

实测在 Debian 上需要（其它发行版对应替换）：

```bash
sudo apt-get update
sudo apt-get install -y \
    build-essential curl git unzip \
    pkg-config libgmp-dev libmpfr-dev libsqlite3-dev zlib1g-dev \
    cmake python3 bubblewrap
```

注意两个坑：

- **`autoconf` / `automake` 版本**。Infer 的 `./autogen.sh` 需要 autoconf ≥ 2.72、automake ≥ 1.17。
  Debian 自带的可能太老。如果 `autogen.sh` 报错，源码装到用户目录即可（不要动系统的）：

  ```bash
  T=$REPO/tools; mkdir -p $T/local $T/build && cd $T/build
  curl -fsSL https://ftp.gnu.org/gnu/autoconf/autoconf-2.72.tar.gz | tar xz
  (cd autoconf-2.72 && ./configure --prefix=$T/local && make -s install)
  curl -fsSL https://ftp.gnu.org/gnu/automake/automake-1.17.tar.gz | tar xz
  (cd automake-1.17 && ./configure --prefix=$T/local && make -s install)
  export PATH=$T/local/bin:$PATH        # 后面每一步都要带上
  ```

- **`bubblewrap`**。opam 2.5 默认用它做沙箱。装不了的话给 opam 加 `--disable-sandboxing`。

---

## 2. 拿到发行版（提供预编译的 clang）

```bash
REPO=~/VSCode/MemHint
T=$REPO/tools && mkdir -p $T && cd $T
curl -fsSL -o infer.tar.xz \
  https://github.com/facebook/infer/releases/download/v1.2.0/infer-linux-x86_64-v1.2.0.tar.xz
tar xf infer.tar.xz && mv infer-linux-x86_64-v1.2.0 infer && rm infer.tar.xz
$T/infer/bin/infer --version      # 期望 Infer version v1.2.0
```

`tools/infer/` 从此就是**未打补丁的官方版**，留着做对照组，不要删。

## 3. 拿到源码（必须是同一个 commit）

```bash
cd $T
git clone https://github.com/facebook/infer.git infer-src
cd infer-src && git checkout 4c53e80cac9ab1066920593fc1ef4d81e8d2e0c6   # = tag v1.2.0
```

用 tag `v1.2.0` 也可以，`4c53e80` 就是它指向的 commit。**版本必须和第 2 步的发行版一致**，
否则链接进来的 clang 插件和源码对不上。

## 4. 把发行版的 clang 链接进源码树

这是整件事的关键——让 make 相信 clang 已经装好了，跳过 3 小时的 LLVM 编译。

```bash
cd $T/infer-src/facebook-clang-plugins
rmdir clang/install libtooling/build 2>/dev/null   # 如果是空目录
ln -sfn $T/infer/lib/infer/facebook-clang-plugins/clang/install    clang/install
ln -sfn $T/infer/lib/infer/facebook-clang-plugins/libtooling/build libtooling/build
./clang/setup.sh --only-record-install             # 写 installed.version
ls -la clang/install libtooling/build              # 确认是两个软链接
```

## 5. 装 opam 和 OCaml switch

```bash
mkdir -p $T/opam-bin && cd $T/opam-bin
curl -fsSL -o opam https://github.com/ocaml/opam/releases/download/2.5.2/opam-2.5.2-x86_64-linux
chmod +x opam

export PATH=$T/local/bin:$T/opam-bin:$PATH
export OPAMROOT=$T/opam-root OPAMYES=1 OPAMJOBS=8

opam init --bare --no-setup
cd $T/infer-src
./build-infer.sh --only-setup-opam -y clang        # 建 4.14.0+flambda switch，约 8 分钟
```

## 6. 装依赖（**这一步有坑，照着做**）

Infer 1.2.0 自带的 `opam/infer.opam.locked` 已经**整体无法求解**了：它钉的
`cmdliner 1.2.0`、`conf-gmp 4`、`mtime 2.0.0` 等版本已从 opam 仓库下架。

```bash
cd $T/infer-src
eval $(opam env --switch=4.14.0+flambda --set-switch)

# 6a. 先不带锁文件装一遍
opam install --deps-only ./opam/infer.opam --assume-depexts        # 约 8 分钟

# 6b. atd 系列必须钉回 2.15.0
#     （atdgen 4.x 改了 findlib 库名，yojson 3.0 删了 Yojson.Safe.start_any_variant，都会直接编译失败）
URL=https://github.com/ahrefs/atd/archive/refs/tags/2.15.0.tar.gz
opam pin add -n atd.2.15.0            $URL
opam pin add -n atdgen-runtime.2.15.0 $URL
opam pin add -n atdgen.2.15.0         $URL
opam install atd.2.15.0 atdgen-runtime.2.15.0 atdgen.2.15.0

# 6c. 其余库按锁文件版本装回去。逐个装，不要一次全给
#     （一次全给的话，只要有一个版本不存在，整批都会失败）
for p in yojson.2.1.2 ppx_deriving.5.2.1 ppxlib.0.32.0 sedlex.3.2 sqlite3.5.1.0 \
         re.1.11.0 ctypes.0.22.0 integers.0.7.0 zarith.1.13 base.v0.15.1 \
         spawn.v0.15.1 camlzip.1.11 extlib.1.7.9 iter.1.8 stdcompat.19 \
         ocamlgraph.2.1.0 ppx_blob.0.7.2 cppo.1.6.9 num.1.5 base64.3.5.1; do
  opam install $p >/dev/null 2>&1 && echo "ok   $p" || echo "SKIP $p"
done
```

`mtime.2.0.0` 在仓库里已经没有了，SKIP 掉不影响。

**如果第 6b 步之前已经跑过 make**：用错版本的 atdgen 生成的文件会留在树里，必须清掉：

```bash
git clean -fdx infer/src/atd
```

## 7. 打补丁

```bash
cd $T/infer-src
git apply --check $REPO/notes/infer-arg-models.patch && \
git apply         $REPO/notes/infer-arg-models.patch
git diff --stat    # 期望 3 个文件：Config.ml / Config.mli / PulseModelsC.ml
```

`--check` 先跑一遍是为了在真正改动前确认 commit 对得上。如果报 hunk 失败，回去检查第 3 步的 commit。

## 8. 编译

```bash
cd $T/infer-src
eval $(opam env --switch=4.14.0+flambda --set-switch)
./autogen.sh
./configure --disable-java-analyzers --disable-erlang-analyzers \
            --disable-hack-analyzers --disable-python-analyzers
make -j8 opt          # 首次约 8 分钟，之后改一个文件增量 2-5 分钟
```

产物：`$T/infer-src/infer/bin/infer`（约 164 MB）。它会自己找到第 4 步链接进来的 clang。

## 9. 验证（重要，别跳）

仓库里有一个自检脚本 `results/infer-arg-models/verify_patch.sh`，12 项检查，退出码 0 表示全过。

```bash
cd $REPO/results/infer-arg-models
./verify_patch.sh $T/infer-src/infer/bin/infer
echo "exit=$?"        # 0 = 全过
```

期望输出：

```
=== binary under test: .../tools/infer-src/infer/bin/infer
Infer version v1.2.0-4c53e80

PASS  option --pulse-model-free-arg-pattern is present
PASS  option --pulse-model-alloc-arg-pattern is present

PASS  malformed N:regex is rejected

--- patch 1: --pulse-model-free-arg-pattern (argn.c)
PASS  only leak_none is reported (allocator model alone) (1 report(s))
PASS  native free model on a 2nd-arg API misfires (5 report(s))
PASS  arg-position free model fixes it (1 report(s))

--- patch 2: --pulse-model-alloc-arg-pattern (out_arg.c)
PASS  no models -> nothing found (all callees are extern) (0 report(s))
PASS  return-value acquisition still works (1 report(s))
PASS  out-parameter acquisition finds 5 leaks (5 report(s))
PASS  with releases configured, only the 6 real leaks remain (6 report(s))
PASS  a scalar at the configured index acquires nothing (0 report(s))
PASS  a void* at the configured index acquires nothing (0 report(s))

=== 12 passed, 0 failed
```

脚本查的是四类东西：

| 检查 | 说明 |
|---|---|
| 选项存在 | `infer analyze --help` 里有没有这两个新选项 |
| 参数解析 | 传个非法的 `N:regex` 要报 UserError，不能静默忽略 |
| 补丁 1 的行为 | 用原生 free 模型建模"第二个参数才是指针"的接口会产生 4 个假泄漏 + 1 个对 ctx 的 use-after-free；换成 `--pulse-model-free-arg-pattern 1:...` 后只剩那一个真泄漏 |
| 补丁 2 的行为 | 出参分配能报出 5 个泄漏；配上释放模型后只剩 6 个真的；把索引配到标量或 `void *` 上不产生任何分配（形状检查） |

### 9.1 反向验证（确认脚本真的在测东西）

拿**没打补丁**的官方二进制跑同一个脚本，必须失败：

```bash
./verify_patch.sh $T/infer/bin/infer
echo "exit=$?"        # 期望 1
```

```
FAIL  option --pulse-model-free-arg-pattern is present
        not in 'infer analyze --help' -- this binary is not patched
FAIL  option --pulse-model-alloc-arg-pattern is present
        not in 'infer analyze --help' -- this binary is not patched

=== 0 passed, 2 failed -- the options are missing, so the behaviour checks
    were skipped (they would pass vacuously on an empty report).
```

脚本在选项缺失时会**提前退出**，而不是继续跑。原因：官方二进制遇到未知选项直接退出、
不写 report.json，于是所有"期望 0 条报告"的检查都会因为错误的理由通过——那样的全绿是骗人的。
这两步都做完，才算真的确认补丁生效了。

### 9.2 Infer 自己的回归测试（可选，约 3 分钟）

```bash
cd $T/infer-src
source $T/infer-env.sh 2>/dev/null || eval $(opam env --switch=4.14.0+flambda --set-switch)
make direct_c_pulse_test direct_cpp_pulse_test
```

两个都应该 SUCCESS。C 那组会拿 128 条期望告警逐字比对，一个字不同都会失败——
这验证的是"补丁没有改变未配置时的行为"。

`make ocaml_unit_test` 会失败，但**在没打补丁的原始树上也一样失败**（dune 的 inline-test runner
把 `inline-test-runner` 当成 Infer 的位置参数），与补丁无关。要自己确认的话：
`git stash && make ocaml_unit_test; git stash pop`，失败方式相同。

### 9.3 Python 侧

补丁的流水线接线也有单测：

```bash
cd $REPO && .venv/bin/python -m pytest tests -q      # 期望 7 passed
```

其中三个是这次加的：出参分配器的 Eq.1 校验、`OwnershipModel` 向调用方的传播、
以及四种摘要形状分别注入到哪个选项。

## 10. 以后重新编译（不需要 opam）

改了 OCaml 源码之后，把下面存成 `$T/infer-env.sh`：

```bash
export OPAMROOT=$HOME/VSCode/MemHint/tools/opam-root
export OPAM_SWITCH_PREFIX=$OPAMROOT/4.14.0+flambda
export CAML_LD_LIBRARY_PATH=$OPAM_SWITCH_PREFIX/lib/stublibs:$OPAM_SWITCH_PREFIX/lib/ocaml/stublibs:$OPAM_SWITCH_PREFIX/lib/ocaml
export OCAML_TOPLEVEL_PATH=$OPAM_SWITCH_PREFIX/lib/toplevel
export PATH=$OPAM_SWITCH_PREFIX/bin:$PATH
```

然后 `source $T/infer-env.sh && cd $T/infer-src && make -j8 opt` 即可，不需要 opam 在 PATH 上。

---

## 11. 在流水线里用

机器上会有**两个** Infer，别指错：

| 路径 | 是什么 | 用途 |
|---|---|---|
| `tools/infer/bin/infer` | 官方 1.2.0 | 对照组、`anchored` / `official` 模式 |
| `tools/infer-src/infer/bin/infer` | 补丁版 | `anchored-argn` 模式 |

```bash
python -m memhint stage2 <project> --out <out> --analyzer infer \
    --infer-bin tools/infer-src/infer/bin/infer \
    --infer-pattern-mode anchored-argn
```

`anchored-argn` 模式会把四种摘要形状全部注入：

| 摘要 | 注入到 |
|---|---|
| Allocator / return | `--pulse-model-alloc-pattern` |
| Allocator / argN | `--pulse-model-alloc-arg-pattern N:re` |
| Deallocator / arg0 | `--pulse-model-free-pattern` |
| Deallocator / argN (N≥1) | `--pulse-model-free-arg-pattern N:re` |

用官方二进制配 `anchored-argn` 会直接报"未知选项"退出——不会静默出错，但会白跑一轮 capture。

## 12. 三个容易踩的运行期坑

1. **`--results-dir` 不能跨 `infer analyze` 复用**，会有状态污染，产生完全虚假的对比结果。
   每个配置要么用新目录，要么从 capture 目录拷一份。
2. **`--keep-going` 会吞掉编译错误**。capture 完必须查错误数，过程数正常不等于抓取正确：
   ```bash
   sqlite3 <results-dir>/capture.db "select count(*) from source_files; select count(*) from procedures;"
   grep -c "error:" <capture log>
   ```
3. **Pulse 并行分析本身有 ±15-20 条告警的抖动**。比较两个配置时不要看总数，
   要看可归因的子集（比如"调用了这些被建模函数的那些函数里，告警变化如何"）。

## 13. 参考

- 完整设计与实测记录：`notes/infer-arg-models.md`
- 补丁：`notes/infer-arg-models.patch`、`notes/infer-argfile-transport.patch`（后者的设计与验证：`notes/infer-argfile-transport.md`）
- 玩具用例、自检脚本与期望输出：`results/infer-arg-models/`（`verify_patch.sh`、`argn.c`、`out_arg.c`、`run_out_arg.sh`；参数传递：`verify_transport.sh`、`transport_a.c`、`transport_b.c`）
- 与官方 MemHint 实现的对比：`COMPARISON.md` §7
