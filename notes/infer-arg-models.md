# 给 Infer 加按参数位置的分配 / 释放模型

> 2026-09-08。两轮改动，同一份补丁：
> **第一轮** `--pulse-model-free-arg-pattern N:regex`——释放第 N 个参数而不只是第一个；
> **第二轮** `--pulse-model-alloc-arg-pattern N:regex`——通过 `T **out` 出参交出新分配的内存，
> 而不只是通过返回值。
> 补丁：`notes/infer-arg-models.patch`（211 行 diff，3 个文件）。
> 玩具用例与输出：`results/infer-arg-models/`。

Infer 的模型机制原本只能表达四种摘要形状里的两种：

| 摘要 | 原生 Infer | 打补丁后 |
|---|---|---|
| Allocator / return | `--pulse-model-alloc-pattern` | 同左 |
| Allocator / argN | **无法表达** | `--pulse-model-alloc-arg-pattern N:re` |
| Deallocator / arg0 | `--pulse-model-free-pattern` | 同左 |
| Deallocator / argN (N≥1) | **无法表达** | `--pulse-model-free-arg-pattern N:re` |

两个缺口不对称：**释放模型只能减少报告，分配模型才产生报告**，所以出参分配那一半直接关系到召回。

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

## 4. 玩具用例（`results/infer-arg-models/toy_test.txt`）

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

---

# 第二轮：`--pulse-model-alloc-arg-pattern`（出参分配）

## 7. 为什么需要它

原生 `--pulse-model-alloc-pattern` 只把匹配函数的**返回值**标记为新分配的内存。
`int make(ctx *c, thing **out)` 这种"返回状态码、指针从出参出来"的接口一个也建模不了。
这类接口在系统级 C 代码里比 `p = alloc(n)` 更常见（`getaddrinfo`、`posix_memalign`、
`sqlite3_open`、绝大多数 COM/句柄风格的 API 都是这个形状）。

而且**这一半才影响召回**：释放模型只会消掉报告，分配模型才产生报告。第一轮补丁在 Vim 上召回一点没变，
根源就在这里——它改的是"减法"那一半。

## 8. 补丁内容（在第一轮基础上）

| 文件 | 改动 |
|---|---|
| `infer/src/base/Config.ml` | 新选项 `--pulse-model-alloc-arg-pattern N:regex`；把两个选项共用的 `N:regex` 解析抽成 `parse_arg_index_patterns` |
| `infer/src/base/Config.mli` | 导出 `val pulse_model_alloc_arg_pattern : (int * Str.regexp) list` |
| `infer/src/pulse/PulseModelsC.ml` | 新模型 `alloc_out_arg` + `alloc_arg_matchers`（N 支持 0–7） |

模型本体只有十行，全部复用现成的 DSL 组合子：

```ocaml
let is_out_param_typ (typ : Typ.t) =
  match typ.desc with Typ.Tptr (pointee, _) -> Typ.is_pointer pointee | _ -> false

let alloc_out_arg (out_arg : DSL.aval FuncArg.t) : model =
  let open DSL.Syntax in
  start_model
  @@ let* {callee_procname} = get_data in
     if not (is_out_param_typ out_arg.FuncArg.typ) then ret ()
     else
       let desc = Procname.to_string callee_procname in
       let* acquired = mk_fresh ~model_desc:desc ~more:"(out parameter)" () in
       let* () = allocation (CustomMalloc callee_procname) acquired in
       let* () = and_positive acquired in
       write_deref ~ref:out_arg.FuncArg.arg_payload ~obj:acquired
```

三个设计选择值得说明：

1. **类型形状检查**（`is_out_param_typ`）。指定的参数必须真的是"指针的指针"，否则模型什么都不做。
   这样把索引配错不会把无关的值标成新分配的内存——请求里的 Case D/D′ 就是测这个。
   代价：声明成 `void *out`（不透明句柄）的出参认不出来，这是有意的取舍。
2. **只按名字匹配，不按类型猜**。任何 `T **` 参数都不会自动获得分配语义，必须显式配了正则才生效
   （Case G）。
3. **返回值不动**。模型不给返回值任何约束，所以 `if (make(&p) != 0)` 两条分支 Pulse 都会走。

## 9. 玩具用例（`results/infer-arg-models/out_arg.c` + `run_out_arg.sh`）

八种配置跑同一份 128 行的通用 C 代码，输出在 `toy_test_out_arg.txt`：

| 配置 | 报告 | 说明 |
|---|---|---|
| baseline（不配任何模型） | 0 | 全是 `extern` 声明，Pulse 无从知道语义 |
| 只配返回值分配 | 1（`leak_return`） | 原有能力未受影响 |
| 只配 argN 释放 | 0 | 释放模型不产生报告 |
| 只配出参分配 | 5 | `leak_out0` `leak_out1` `leak_out0_of_two_args` `leak_only_first_of_two` `leak_conditional` |
| 全配 | 6 | 上面 5 个 + `leak_return`；`no_leak_*` 一个都没报 |
| 索引配到标量参数上 | 0 | Case D：`0:make_b`（arg0 是 `int`）不产生分配 |
| 索引配到 `void *` 参数上 | 0 | Case D′：形状检查要求"指针的指针" |
| 同名函数既配分配又配释放 | 4 × USE_AFTER_FREE | Case 冲突，见下 |

逐条对应验收标准：

- Case A（返回值分配）：`leak_return` 报，`no_leak_return` 不报。
- Case B（出参分配 + 泄漏）：三个位置（arg0、arg1、两参数中的 arg0）都报。
- Case C（出参分配 + 释放）：`no_leak_out0` / `no_leak_out1` 不报。
- Case D/D′（索引错）：0 条。
- Case E（多个出参）：`make_two` 只配了 arg0，于是 `leak_only_first_of_two` 报（`*a` 泄漏），
  `no_leak_first_of_two` 不报，说明索引精确选中了目的地，`*b` 完全没被碰。
- Case F（只在成功时写出参）：`leak_conditional` 被报了。**这是有意保留的限制**，见 §11。
- Case G（无关的 `T **` 参数）：`borrow_ref` 没配正则，不报。
- 回归（释放 arg0/1/2）：三个 `no_leak_free_arg*` 都不报。

## 10. 回归测试与构建

| 项目 | 结果 |
|---|---|
| 改动文件 | 3（Config.ml / Config.mli / PulseModelsC.ml） |
| 新增用例 | C 夹具 8 组配置 + Python 单测 3 个（`tests/test_analysis.py`） |
| `make direct_c_pulse_test` | 通过，128 条期望告警逐字相同 |
| `make direct_cpp_pulse_test` | 通过（102 s） |
| `make ocaml_unit_test` | 失败，但**在未打补丁的原始树上同样失败**（dune 的 inline-test runner 把 `inline-test-runner` 当成 Infer 的位置参数），与本补丁无关 |
| `pytest tests` | 7 passed |
| 增量编译 | `make -j8 opt` 约 5 分钟 |
| 默认是否启用 | **否**，不配选项时行为逐字不变 |

构建环境：opam 二进制本身已经不在机器上了，但 switch 还在。用下面这个脚本
（`tools/infer-env.sh`，`tools/` 在 .gitignore 里所以没入库）直接导出 switch 的环境，
`source tools/infer-env.sh && make -j8 opt` 就能增量重编，不需要 opam：

```bash
export OPAMROOT=<repo>/tools/opam-root
export OPAM_SWITCH_PREFIX=$OPAMROOT/4.14.0+flambda
export CAML_LD_LIBRARY_PATH=$OPAM_SWITCH_PREFIX/lib/stublibs:$OPAM_SWITCH_PREFIX/lib/ocaml/stublibs:$OPAM_SWITCH_PREFIX/lib/ocaml
export OCAML_TOPLEVEL_PATH=$OPAM_SWITCH_PREFIX/lib/toplevel
export PATH=$OPAM_SWITCH_PREFIX/bin:$PATH
```

## 11. 已知限制

1. **条件写出参**（Case F）。模型无条件写 `*out`，不看返回的状态码。
   `if (make(&p) == 0) use(p);` 里失败分支上 Pulse 也认为 `p` 被分配了，可能产生误报。
   要做对需要状态码敏感的建模，超出了这次的范围。我们的 Z3 层（`leak_feasible`）有同样的行为，
   两层是一致的，最终由 Stage 3 的 LLM 复核来兜。
2. **不透明出参**。`void **` 可以，`void *`（把 `T**` 藏在 `void*` 后面）认不出来。
3. **一个函数只有一个模型**，而且 Infer 先试释放匹配器。同一个名字既进分配规则又进释放规则时，
   分配语义被静默丢弃，调用方随后看起来像 use-after-free（玩具用例最后一组就是这个）。
   `memhint/analyzers/infer.py` 在注入前会 `log.warning` 列出这类函数名。
4. **参数索引上限 7**，超出直接 `L.die UserError`。组合子是手写展开的，需要更多再加即可。

## 12. 接入流水线

- Stage 1（`memhint/summarize.py`）：提示词和解析器现在接受 `Allocator / argN`，并明确要求
  只有真的写 `T **out` 才用 argN，借来的/内部缓存的指针不算。
- Stage 1 校验（`memhint/analysis.py`，Eq.1）：`_is_allocator(name, depth, idx)` 对出参分配器改判
  "分配是否能沿可行路径到达 `*out` 的写入"，而不是"到达 return"。直接写（`*out = malloc(...)`）和
  经局部变量中转（`p = malloc(); ...; *out = p;`）都认；写借来的指针（`*out = c->cached`）和
  内部分配后自己释放（`internal`）都被拒。
- `OwnershipModel` 新增 `out_allocators`，`track()` 把 `g(&p)`（g 在索引 i 上出参分配）算作 `p` 的分配点，
  于是 Eq.5 的泄漏可行性判定对调用方也生效。
- Stage 2（`memhint/analyzers/infer.py`）：`anchored-argn` 模式下四种形状全部注入。
  注意 `anchored` 和 `official` 两个模式**行为未变**——它们要对得上原生 Infer 和官方实现，
  已记录的 Vim 结果不受影响。

## 13. 在 Vim 上的预期收益：零

`output/vim_9_2_0015/hints.json` 里 340 个分配器**全部**是 `target: return`，非 arg0 释放器 37 个。
Vim 的风格就是 `p = alloc(n)`，没有出参分配器。所以这个补丁在 Vim 上不会新增任何召回——
这一点必须说清楚，它的价值在于 `status = make(ctx, &out)` 风格占主导的代码库。

需要注意的是，旧的 `hints.json` 是在 Stage 1 还**不允许** `Allocator/argN` 的提示词下生成的
（老的解析器会直接丢弃这种摘要），所以"Vim 里 0 个出参分配器"严格说是"在旧提示词下 0 个"。
要拿到无偏的数字得用新提示词重跑 Stage 1；本轮没有重跑，因为 Vim 的编码风格已经足以支持这个结论。
