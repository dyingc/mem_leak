# 给 Infer 加 `--pulse-model-free-arg-pattern`：可行性验证记录

> 2026-09-08。目的：验证"改 Infer 的 OCaml 源码、加一个能指定被释放参数位置的模型开关、
> 复用发行版自带的 clang 重新编译"这条路是否走得通，以及它在 Vim 上的实际效果。
> 补丁：`notes/infer-free-arg-pattern.patch`（97 行 diff，3 个文件）。
> 玩具用例与输出：`results/infer-free-arg/`。

## 1. 问题

Infer 的 `--pulse-model-free-pattern` 把匹配的函数建模为"释放**第一个**参数"，没有别的位置可选。
我们在 Vim 上验证过的 153 个释放函数里有 37 个释放的不是第一个参数（arg1 20、arg2 10、arg3 3、
arg4-7 各 1），这些一直注入不进 Infer。Oracle 这类"描述符在前、指针在后"的接口会把比例反过来。

## 2. 补丁内容

三个文件，共 56 行新增：

| 文件 | 改动 |
|---|---|
| `infer/src/base/Config.ml` | 新选项 `--pulse-model-free-arg-pattern N:regex`（`mk_string_list`，可重复），后处理成 `(int * Str.regexp) list` |
| `infer/src/base/Config.mli` | 导出 `val pulse_model_free_arg_pattern : (int * Str.regexp) list` |
| `infer/src/pulse/PulseModelsC.ml` | 在 matcher 列表前插入 `free_arg_matchers`：对每个 `(n, regex)` 生成 `<>$ any_arg $+ … $+ capt_arg $+...$--> free`，`any_arg` 重复 n 次；n 支持 0–7 |

核心就是把原来那一行

```ocaml
+match_regexp_opt Config.pulse_model_free_pattern <>$ capt_arg $+...$--> free
```

换成按位置跳过前 n 个参数：

```ocaml
| 1 -> +match_regexp r <>$ any_arg $+ capt_arg $+...$--> free
| 2 -> +match_regexp r <>$ any_arg $+ any_arg $+ capt_arg $+...$--> free
```

`any_arg` 和 `$+` 都是 `ProcnameDispatcher.Call` 里现成的组合子，没有引入新概念。

## 3. 重新编译：实际走通的路径

关键点：**发行版 tarball 里已经带了编译好的 clang（909 MB）和 AST 插件**，所以只需要编译 OCaml 部分。

```bash
# 1. 源码
git clone --depth 1 --branch v1.2.0 https://github.com/facebook/infer.git tools/infer-src

# 2. 把发行版的 clang 和插件链接进源码树，并"假装"已安装
cd tools/infer-src/facebook-clang-plugins
ln -s <release>/lib/infer/facebook-clang-plugins/clang/install   clang/install
ln -s <release>/lib/infer/facebook-clang-plugins/libtooling/build libtooling/build
clang/setup.sh --only-record-install         # 写 installed.version，make 就不会去编 clang

# 3. opam（用户目录内，不碰系统）
opam init --bare --no-setup; ./build-infer.sh --only-setup-opam -y clang   # 建 4.14.0+flambda switch

# 4. 依赖
opam install --deps-only ./opam/infer.opam --assume-depexts       # 见下面"坑"

# 5. 编译
./autogen.sh && ./configure --disable-java-analyzers --disable-erlang-analyzers \
    --disable-hack-analyzers --disable-python-analyzers
make -j8 opt            # 产物 infer/bin/infer，可直接用（它会找到链接进来的 clang）
```

耗时（12 核、28 GB）：opam switch + 编译 OCaml 4.14 约 8 分钟；依赖 135 个包约 8 分钟；
Infer 本体首次全量编译约 8 分钟（其中 C++ 插件针对自带 clang 头文件重编 52 秒），改一个文件后增量编译 2 分钟。
**全程没有编译 LLVM/clang。**

### 踩过的坑（都是版本漂移，与补丁本身无关）

1. `opam/infer.opam.locked` 钉的 `cmdliner 1.2.0`、`conf-gmp 4`、`atdgen 2.15.0`、`mtime 2.0.0` 等
   已从 opam 仓库下架，锁文件整体无法求解。用 `--no-opam-lock` 解析会拿到过新的库。
2. `atdgen 4.x` 的库名变了（没有 `atdgen` findlib 库），`yojson 3.0` 删了 `Yojson.Safe.start_any_variant`，
   直接编译失败。解决：`opam pin add atd.2.15.0 / atdgen.2.15.0 / atdgen-runtime.2.15.0 <GitHub tag tarball>`，
   再把 `yojson ppx_deriving ppxlib sedlex sqlite3 re ctypes integers zarith base spawn …` 逐个装回锁文件版本
   （仓库里还在的都装回去，`mtime 2.0.0` 已不存在就跳过）。
3. 用错误的 atdgen 版本生成过的 `infer/src/atd/clang_ast_*.ml` 会残留，换回 2.15.0 后要 `git clean` 掉重新生成。
4. `PulseModelsC.ml` 里 `L` 没有打开，报错要用 `Logging.die`。

一句话：**编译门槛不在 clang，而在 opam 生态的版本漂移；从零到可用的 Infer 二进制约 40 分钟。**

## 4. 玩具用例（`results/infer-free-arg/toy_test.txt`）

```c
extern void *my_alloc(int n);                          /* 不可见的分配器 */
extern void my_free2(struct ctx *c, void *p);          /* 释放第 2 个参数 */
extern void my_free3(struct ctx *c, int flags, void *p);
void leak_none(struct ctx *c)   { void *p = my_alloc(4); (void)p; }          /* 真泄漏 */
void ok_free2(struct ctx *c)    { void *p = my_alloc(4); my_free2(c, p); }
void ok_free3(struct ctx *c)    { void *p = my_alloc(4); my_free3(c, 0, p); }
void use_ctx_after(struct ctx *c){ void *p = my_alloc(4); my_free2(c, p); my_free2(c, my_alloc(2)); }
```

| 配置 | 报告 |
|---|---|
| 只有 alloc 模式 | `leak_none`（正确） |
| 加原生 `--pulse-model-free-pattern` 匹配 `my_free2\|my_free3` | `leak_none` + **3 个假泄漏** + **1 个对 `ctx` 的 USE_AFTER_FREE** |
| 加新的 `--pulse-model-free-arg-pattern 1:my_free2` `2:my_free3` | `leak_none`（正确） |

第二行就是把描述符在前的释放接口交给原生开关的后果：释放没被认出来，描述符反而被当成已释放。

## 5. 在 Vim 上的效果

三组共用同一份 capture（编译数据库方式，151 文件 / 10,694 过程），都注入 340 个分配器 + 116 个 arg0 释放器，
差别只在那 37 个非 arg0 释放器（35 个函数）怎么处理：

| 组 | 非 arg0 释放器的处理 | 二进制 | 泄漏告警 |
|---|---|---|---|
| control (`infer-cdb`) | 不注入（Pulse 只能靠分析它们的函数体） | 发行版 1.2.0 | 308（去重 289） |
| shim (`infer-shim2`) | capture 时用宏把调用改写成 `free(argN)` | 发行版 1.2.0 | 290（去重 277） |
| argn (`infer-argn`) | `--pulse-model-free-arg-pattern` 注入 | 补丁版 | 295（去重 281） |

### 5.1 告警层面

先量噪声：同一配置、两份不同 capture（`infer-refF` 用 make 抓取、control 用编译数据库抓取）的泄漏告警
去重后 284 vs 289，交集 269，各有 15 / 20 条只出现在一边。**Pulse 并行分析本身就有 ±15-20 条的抖动。**

补丁版 vs control：交集 260，只在补丁版 21 条、只在 control 29 条，净减 8——落在噪声带内。
所以不能拿总数说事，得看**可归因**的部分：Vim 里调用这 35 个函数的有 112 个函数，control 在它们
里面报了 22 条泄漏，补丁版消掉了其中 6 条，逐条核对全部是误报：

| 函数 | 行 | control 报什么 | 为什么是误报 |
|---|---|---|---|
| `nextwild` | 317 | `tmp = vim_strnsave(...)` 未释放 | `tmp` 作为第 2 个参数传给 `ExpandOne()`，由它释放 |
| `nextwild` | 382 | `ExpandOne()` 的返回值未释放 | `(void)ExpandOne(xp, NULL, NULL, 0, WILD_FREE)`，是释放调用 |
| `f_getcompletion` | 4660 | 同上 | 同上 |
| `f_glob` | 1354 | 同上 | 同上 |
| `f_expand` | 4981 | 同上 | 同上 |
| `cmdline_wildchar_complete` | 1013 | 同上 | 同上 |

注意后五条消失的机理：`ExpandOne` 既是分配器（返回值）又是释放器（arg2），补丁版里 free 匹配器排在前面，
它被建模成"只释放 arg2"，分配器身份丢了。这恰好消掉了 `WILD_FREE` 调用的误报，但也意味着 `ExpandOne`
真正返回分配内存的路径不再被跟踪——**一个函数只能有一种模型**，这是 Infer 模式机制的固有限制，
影子方案同样如此。

### 5.2 bug 层面（Z3 + gpt-5.6-luna + 上游真值）

前四列是"我们报了什么、对不对"（精确率方向），最后一列是"上游该找的找到没有"（召回方向），
两者分母不同：**报告函数 = 命中上游 + 未命中；未命中 = 人工真 + 人工假**；而"窗口命中"的分母是上游那
19 个补丁，与本行其它数字无关。

| 组 | 告警 | Z3 后 | 报告函数 | 命中上游（函数／补丁） | 未命中 | 人工真 | 人工假 | 窗口命中 |
|---|---|---|---|---|---|---|---|---|
| refF（make capture） | 298 | 267 | 24 | 16 / 16 | 8 | 5 | 3 | 8/19 |
| control | 308 | 280 | 27 | 16 / 16 | 11 | 5 | 6 | 8/19 |
| 补丁版 | 295 | 261 | 24 | 16 / 16 | 8 | 5 | 3 | 8/19 |
| 影子版 | 290 | 260 | 24 | 16 / 16 | 8 | 5 | 3 | 8/19 |

（"命中上游"两个数都是 16 是巧合：每个命中函数恰好对应一个不同的上游补丁。）

四组漏掉的都是同样 11 个窗口补丁，即我们一条告警都没出过的函数，例如 `ExpandFromContext`（9.2.0055）、
`invoke_sync_listeners`（9.2.0065）、`eval_dict`（9.2.0079）、`win_init_empty`（9.2.0118）——
其中几个是 CodeQL 那条线报出来的，正是论文说的两个分析器互补。

召回完全不变（同样的 16 个补丁、窗口内 8 个）。影子版和补丁版在 bug 层面完全一致，说明两条路建模出的语义相同。
影子版的分析耗时 4,346 s，是控制组 1,174 s 的 3.7 倍（Pulse 调度了 110 个过程块 vs 66 个）：宏展开把
`free()` 塞进了表达式里，改变了调用图形状；补丁版 1,469 s，与控制组同量级。这也是更倾向于补丁而非影子方案的一个理由。control 多出的 3 个误报（`f_listener_add`、
`option_set_callback_func`、`qf_setprop_qftf`，都是 `get_callback()` 相关）在 refF 和补丁版里都没有，
属于运行间抖动，不能记在补丁头上。

### 5.3 结论

- **机制层面**：补丁按预期工作，玩具用例干净利落；Infer 那个"只能释放第一个参数"的限制被解除了。
- **在 Vim 上收益很小**，原因很清楚：那 37 个非 arg0 释放器全部有可见函数体，Pulse 本来就能分析它们；
  模型只是把"分析函数体"换成"按声明相信"，两者在 Vim 上结论一致。这个补丁的价值在于函数体
  **不可见或 Pulse 分析不动**的场景——恰恰是 Oracle 那种堆管理器在另一个组件里、接口"描述符在前"的情况。
- 所以对 Oracle 的建议不变，只是从"估算"变成了"有现成补丁和 40 分钟的构建配方"。

## 6. 影子方案的坑（预处理器 `-include`）

第一次用 `-include shim.h` 把 35 个函数定义成函数式宏时，capture 报了 **2,731 个编译错误**：
`proto/*.pro` 里这些函数的**原型声明**也被宏展开了。Infer 的 `--keep-going` 把错误吞掉继续抓取，
过程数看起来正常，结果其实不可用。修法是让 shim 头文件先 `#include "vim.h"`（把所有原型带进来）再定义宏，
并且只对 `src/` 根目录下的编译单元生效（libvterm、xdiff 不含 vim.h）。副作用：main.c 依赖先 `#define EXTERN`
再包含 vim.h 来定义全局变量，预包含之后 92 个 `__infer_globals_initializer_*` 消失；只影响全局初始化器，不影响函数。

教训：**capture 的错误数必须查，过程数一致不等于抓取正确。**
