# 与 MemHint 官方仓库的对照（jiekeshi/MemHint）

> 2026-09-07/08。官方仓库 `https://github.com/jiekeshi/MemHint` 克隆在 `tools/MemHint/`（不入库）。
> 目的：(1) 确认我们的复现总体正确；(2) 查明 Infer 这条线为什么与论文差距巨大。
> 三个阶段的逐行对照原稿在 `notes/compare_stage{1,2_codeql,3}.md`。

## 0. 结论先行

| 问题 | 结论 |
|---|---|
| 复现总体是否正确 | **是**。Stage 1 抽取/预过滤/提示词、Stage 2 CodeQL 注入与查询、Stage 3 LLM 验证都与官方等价；Stage 1 的 Z3 校验和 Stage 3 的 Z3 过滤我们按论文公式实现，官方代码则是启发式，差异在官方一侧（见 §2、§4）。 |
| Infer 是否写错了 | **是，两处。** (a) 正则语法：Infer 用 OCaml `Str`，"或"是 `\|`，我照论文附录写成 `^(a\|b)$` 的 POSIX 形式，**一个函数都没匹配上**，等于没有注入摘要；(b) 之前"注入后 102 条 vs 基线 21 条"的差异不是摘要造成的（见 §3.4）。修正后 Infer 线复现成功：告警 332 条，LLM 确认 24 个函数 / 32 条，其中 24 条命中上游修复（16 个不同补丁），另发现 1 个上游未修的真泄漏（`json_encode_lsp_msg`）。 |
| 论文数字为何仍高于我们 | 官方 Stage 1 校验器在 Vim 上放行 567 个 Allocator（含 360 个无任何分配证据的"weak"通过）而只放行 5 个 Deallocator；再加 Infer 模式是**前缀匹配**且不加锚点，`alloc` 会同时命中 `alloc_does_fail`、`alloc_cmdbuff`……告警基数因此膨胀，Stage 3 再用"建模失败即丢弃"的策略砍掉 86-91%。 |

## 1. 官方仓库概况

- 结构：`main.py` → `src/core/pipeline.py`（Phase 1-6）；`src/tree_sitter_parser.py`；`src/llm_client.py`（Gemini）；`src/symbolic/z3_solver.py`（摘要校验 + 路径可行性）；`src/analyzer/adapters.py`（CodeQL/Infer）；`src/verify_bugs_llm.py`。
- 模型：Stage 1 `gemini-3-flash-preview`，Stage 3 `gemini-3.1-pro-preview`；批大小 20。
- 构建：Vim 是 `make clean; make`（configure 自动检测，GUI 随机器而定）。Infer 用 `infer run --keep-going --pulse-only --debug-level 2 -- make`。
- 没有自定义 Infer 构建（README 提到的 `PulseEnhancedModels.ml` 在仓库里不存在，代码路径也未引用）。
- 仓库不含任何输出数据，无法直接拿到论文的 1,011/1,032 条告警清单。

## 2. Stage 1（摘要生成）对照

| 环节 | 官方 | 我们 | 判定 |
|---|---|---|---|
| 抽取 | tree-sitter `function_definition`；`.h` 用 C++ parser；只转换 function-like 宏 | 同；`.h` 用 C parser；另外收录展开体含调用的 object-like 宏（+627）；`#if` 混入表达式时抹掉条件编译行重解析（+73） | 等价。#Extr. 11,956 − 627 − 73 ≈ 11,256 vs 论文 11,071 |
| 预过滤 | 剔除 `main/_main/wmain` 与名含 `test`；宏一律保留；其余签名需含 `*` 或指针 typedef | 同 | 等价。去掉 object-like 宏后 #Cand. 8,691 vs 论文 8,539 |
| 提示词 | `BATCH_HINT_GENERATION_PROMPT` | 逐句相同；callee 上下文同为前 5 个（我们裁剪到 80 行） | 等价 |
| 解析 | 接受 `Allocator+argN`（out-parameter） | 丢弃 `Allocator+argN`（论文只定义 return 形式） | 我们更保守；对 Stage 2 无影响（CodeQL/Infer 模型都只支持返回值形式） |
| Z3 校验 | **启发式**：Allocator = 返回类型含 `*` 且有 `return` 即通过（"validated (weak)"），Z3 门"无证据即保留"；宏 Allocator 因 return_type 为空一律拒绝。Deallocator 必须字面匹配 `\bfree(p)`/`sdsfree`/`zfree`/`decrRefCount`…（Redis 特化名单），`vim_free(p)` 不匹配 → Vim 上几乎全拒 | 按论文 Eq.1/2：acyclic CFG + Z3，Allocator 需"分配→返回"可行路径，Deallocator 需某条可行路径释放参数或别名 | **差异在官方一侧** |

把官方 `HintValidator` 直接跑在我们同一批 1,270 条原始摘要上（`notes/compare_stage1.md` §3.4）：

| 校验器 | 通过 | Allocator | Deallocator |
|---|---|---|---|
| 官方 | 572 | 567（其中 360 条 weak，如 if_py 的 `BufferNew/BufferRepr`） | **5** |
| 我们 | 493 | 340 | 153 |
| 交集 | 324 | 321 | 3 |

所以论文 Table IV 的 688 与我们的 493 数字接近，**构成完全不同**：官方模型里几乎没有 Vim 的释放函数，却混入大量非分配器。这直接决定了 Stage 2 的告警基数。

## 3. Stage 2 对照

### 3.1 CodeQL：等价

- 注入：官方也是写 data extension（`allocationFunctionModel`/`deallocationFunctionModel`），只是直接写进 cpp-all 包的 `ext/` 目录再删掉；我们用独立 model pack + `--model-packs`。行列逐字段一致。
- 查询：官方默认只跑两条硬编码 `.ql`。`ENHANCED_MEMORY_MAY_NOT_BE_FREED.ql` 与标准查询 diff 后只是把 `where` 拆成两个互斥分支，结果集不变；`ENHANCED_MEMORY_NEVER_FREED.ql` 是标准条件加 reason 字符串。带 LLM 过滤谓词的 `*_FILTERED` 版本需要 `use_custom_queries=True`，`main.py`/`run.sh` 都没开。
- 计数：两边都逐条 SARIF result 计数、**都不去重**。REPRODUCTION.md 原先写的"对 (文件,行,规则) 去重"与代码不符，已更正。
- vanilla：官方靠构造参数 `skip_hint_injection=True` 跳过注入，查询不变；我们 `--vanilla` 同理。

### 3.2 Infer：我们错了两处

**(a) 正则语法。** 官方 `adapters.py:1284-1314` 生成的模式是 `name1\|name2\|...`；论文附录 B 印的却是 `'^(a|b|...)$'`。我照附录写成 `^(a|b)$`。用一个小 C 工程实测（`scratchpad/retest/t.c`：`my_alloc` 只有声明没有定义）：

| `--pulse-model-alloc-pattern` | 是否报出 `leak_opaque` |
|---|---|
| 无 | 否 |
| `^(my_alloc\|zzz)$`（我原来的写法，论文附录写法） | **否** |
| `my_alloc\|zzz`（官方写法） | 是 |
| `^\(my_alloc\|zzz\)$`（Str 语法加锚点） | 是 |
| `my_al`（前缀） | 是 |

Infer 1.2.0 用 `Str.regexp` 编译、`Str.string_match … 0` 匹配：`|`、`(`、`)` 是普通字符；匹配是**前缀匹配**；匹配到的函数**即使源码可见也会被模型替换**（`get_static` 返回静态缓冲区却被报成泄漏）。

**(b) `--debug-level 2` 无关。** 官方另加的 `--keep-going --pulse-only --debug-level 2` 只影响日志量和耗时（A 组 1,710 s vs B 组 1,409 s），告警数一样（332 vs 335）。

## 4. Stage 3 对照

- Phase 5：官方也是边变量 + AtMost-1 + alloc/freed/escaped 状态位（与论文 Eq.3-5 一致）。但 (1) **分支条件从未进入编码**（存了 `condition` 文本却不使用），没有论文的 $b_v$；(2) **循环体整体丢弃**（`compound_statement` 返回 None），`#if` 也不处理；(3) 函数按**名字**查找（Vim 大量 static 同名函数会串号）；(4) 建模失败一律**丢弃**：无 ALLOC 节点、找不到变量、无 RETURN 节点（void 函数）、异常、分配写入参数字段。我们的实现：条件变量（单定义变量共享）、循环展开一次、`#if` 当分支、按文件+行定位、建模失败**保留给 LLM**。这就是官方 91% 过滤率 vs 我们 21% 的来源，不是我们编码错误。另发现官方 CFG 的一个 bug：`return/goto` 之后同块还有语句时 `add_edge(None, …)` 抛 `KeyError`，被当作"不可行"过滤。

  把官方 `WarningValidator` 直接跑在我们的告警上（`notes/compare_stage3.md` §1.4）：

  | 输入 | 条数 | 官方保留 | 我们保留 |
  |---|---|---|---|
  | CodeQL + 摘要 | 455 | 243（53%） | 361（79%） |
  | CodeQL vanilla | 268 | 1 | 233 |
  | 旧 Infer + 摘要 | 102 | 55 | 50 |

  官方过滤器在我们的告警上也只过滤 44%，同样复现不出论文的 91%；两边判定不是包含关系。若把官方过滤器套在我们 42 条最终 bug 上会丢 12 条真 bug，包括已验证并起草补丁的 `f_setmatches`、`barline_parse`、`string_reduce`（后者的泄漏 `return` 在 for 循环体内，循环体被丢掉后唯一路径必经 `remove_funccal()`，被判 UNSAT）。所以不建议为对齐数字复刻官方的丢弃规则。
- Phase 6：官方一函数一次调用，Gemini 3.1 Pro；我们的提示词逐句照搬，差别是 trace 每步不附 ±2 行代码。等价。
- Infer 告警在 Stage 3 无差异化处理，两边一致。

## 5. Infer 线修正后的结果

统一用同一份 capture（`make clean; infer capture -- make -j8`，GTK 配置，152 文件 / 10,714 过程），`infer analyze --jobs 6`，默认 `--pulse-max-disjuncts 20`。

| 组 | 摘要 | 模式写法 | 额外标志 | 耗时 | MEMORY_LEAK_C |
|---|---|---|---|---|---|
| 旧 `infer` | 我们的 493 | `^(a\|b)$`（无效） | — | 775 s | 102（见 §3.4 说明） |
| 旧 `infer-vanilla` | 无 | — | — | — | 21 |
| A | 我们的 493 | `a\|b`（官方写法） | `--keep-going --debug-level 2` | 1,710 s | **332** |
| B | 我们的 493 | `a\|b` | 无 | 1,409 s | 335 |
| V0 | 无（重跑基线） | — | 无 | 2,680 s | 26 |
| V1 | 我们的 493 | `^(a\|b)$`（复现旧写法） | 无 | 4,032 s | 21 |
| E | 官方校验器的 572 | `a\|b` | `--keep-going --debug-level 2` | 745 s | **395** |
| F | 我们的 493 | `^\(a\|b\)$`（加锚点，只注入 arg0 释放器） | `--keep-going` | 1,060 s | 298 |

Stage 3（Z3 + gpt-5.6-luna）与上游真值对照：

| 组 | #告警 | Z3 后 | LLM 确认函数 | 报出条数 | 命中上游修复 | 不同补丁数 | 论文窗口(19) | 未命中人工复核 |
|---|---|---|---|---|---|---|---|---|
| A (`infer-refA`) | 332 | 289 | 24 | 32 | 24 | 16 | 8/19 | 8：TP 2（`string_reduce`、`barline_parse`）+ TP 1 新（`json_encode_lsp_msg`）+ TP-FIXED 1 + FP 4 |
| E (`infer-refE`) | 395 | 358 | 28 | 34 | 23 | 17 | 7/19 | 11：TP 3（`string_reduce`、`barline_parse`、`parse_generic_func_type_args` 新，OOM 路径）+ TP-FIXED 2（`clip_wl_init_buffer_store`、`general_beval_cb` 新）+ FP 6 |
| F (`infer-refF`) | 298 | 267 | 24 | 29 | 21 | 16 | 8/19 | 8：TP 3（同上）+ TP-FIXED 2 + FP 3 |
| 论文 Infer | 1,032 | 147 | 25 | 15 确认 | | | | |

"论文窗口"的分母是 19：patch 9.2.0055–9.2.0136 这段日期内的 18 个带编号泄漏修复，加上维护者漏写编号的 `7ed37dc5`（list_extend_func）。与 REPRODUCTION.md 7.1 同一口径。三组精确率（(命中 patch 数 + 人工判真) / 报告函数数）：A 83.3%、E 82.1%、F 87.5%。加锚点的 F 组告警最少、精确率最高、召回与 A 相同，所以本仓库默认用 `anchored` 模式。三组 Stage 3 的 LLM 费用合计 $0.28（大量缓存命中）。

新发现：`json_encode_lsp_msg()`（src/json.c）——`json_encode_gap()` 在已向 `ga` 追加内容后可能 FAIL（例如值里含 funcref），提前 `return NULL` 跳过了 `ga_clear(&ga)`。上游 a96c3bc1 仍存在。CodeQL 没报它（`ga_grow` 系列不在摘要里）。

### 5.1 旧结果"102 vs 21"的真相

V0（干净目录、无摘要）26 条，V1（干净目录、旧的无效写法）21 条，两者交集 18 条——**旧写法和不注入没有区别**（Pulse 在 `--jobs` 并行下本身有 ±5 条的抖动）。旧的 102 条是因为当时 `infer-vanilla` 与 `infer` 两次 `infer analyze` 共用同一个 `--results-dir`，第二次分析在第一次留下的 results.db 上增量跑出来的，与摘要无关。REPRODUCTION.md 5.2 节"Infer 从 21 涨到 102（+386%）"的说法因此作废。

也就是说，此前"Infer + 摘要"报出的 2 个 bug（`eval8`、`ins_compl_infercase_gettext`）来自一次状态污染的运行，不能算复现结果。

## 6. 对我们代码的修改

- `memhint/analyzers/infer.py`：模式改为 OCaml Str 语法；默认 `anchored` 模式（`^\(...\)$`，只注入 arg0 释放器），`official` 模式复刻官方（无锚点、全部名字）；`analyze` 加 `--keep-going`，`--debug-level` 可选；解析 `(custom malloc)` 形式的 qualifier。
- `memhint/pipeline.py` / `cli.py`：`--tag` 给运行目录加后缀；`--hints` 换用别的摘要文件；`--infer-report` 回放已有 report.json；`--infer-pattern-mode`、`--infer-debug-level`。
- `memhint/report.py`：枚举所有 `codeql*`/`infer*` 运行目录。
- `REPRODUCTION.md`：更正"告警去重"的错误描述与 Infer 相关章节（5.2、7.2 差异 2/3/4、11、附）。

## 7. 后续：解除 Infer"只能释放第一个参数"的限制（2026-09-08）

§3.2 提到 Infer 的 free 模式只能建模"释放第一个参数"，我们 153 个验证过的释放器里有 37 个因此注入不进去。
为验证"改 Infer 源码"这条路是否可行，做了两件事，细节在 `notes/infer-free-arg-patch.md`：

1. **预处理器影子方案**：capture 时用 `-include` 把这 35 个函数定义成宏，调用改写为 `free(argN)`。
   第一次因为原型声明也被展开而产生 2,731 个编译错误（Infer 的 `--keep-going` 静默吞掉，过程数看起来正常），
   改为先包含 `vim.h` 再定义宏后 capture 干净。
2. **真正的 Infer 补丁**：新增 `--pulse-model-free-arg-pattern N:regex`（`notes/infer-free-arg-pattern.patch`，
   3 个文件 56 行），复用发行版自带的 clang 重编 OCaml 部分，约 40 分钟。玩具用例：原生开关建模
   `my_free2(ctx, p)` 会报 3 个假泄漏加一个对 `ctx` 的 use-after-free，补丁版只报那个真泄漏。

在 Vim 上（同一份 capture）：补丁版 295 条告警 vs 控制组 308，差异在运行间噪声（±15-20）之内；
可归因的效果是调用这 35 个函数的 22 条告警里消掉 6 条，全部是 `ExpandOne` 相关的误报；
bug 层面召回不变（16 个补丁、窗口内 8/19），误报数与 refF 持平。原因：这些释放器的函数体在 Vim 里可见，
Pulse 本来就能分析；模型的价值在于函数体不可见或分析不动的代码库。补丁已作为 `memhint/analyzers/infer.py`
的 `anchored-argn` 模式接入（需要 `tools/infer-src` 的补丁版二进制）。
