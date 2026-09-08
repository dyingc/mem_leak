# Stage 3（告警验证）：MemHint 官方实现 vs 我们的实现

官方代码：`tools/MemHint/src/symbolic/z3_solver.py`（`WarningValidator`, `CFGBuilder`）、
`tools/MemHint/src/verify_bugs_llm.py`、`tools/MemHint/src/core/pipeline.py` Phase 5/6。
我们的代码：`memhint/verify.py`、`memhint/symbolic.py`、`memhint/cfg.py`、`memhint/analysis.py::leak_feasible`、
`memhint/pipeline.py::Stage3`。

---

## 1. Phase 5：Z3 路径可行性

### 1.1 官方实现实际做了什么

**驱动（pipeline.py:1110-1123）**
- `alloc_funcs` = hints 中所有 Allocator 名 + {malloc, calloc, realloc, strdup}；
  `free_funcs` = hints 中所有 Deallocator 名 + {free}，交给 `WarningValidator(alloc_funcs, free_funcs)`。
- `validate_warnings()` 返回三元组 `(confirmed, filtered, unconfirmed)`。
  只有 `confirmed` 进入 `memory_safety_bugs.json`（= Table V 的 "#Z3"），`filtered` 和 `unconfirmed`
  都不再进入 Phase 6。

**函数定位（z3_solver.py:2100-2107）**
- 按 **函数名** 在 `self.functions` 里查（`functions.get(warning.function_name)`）。
  `self.functions` 是全项目按名合并的 dict，同名函数保留代码更长的那个（pipeline.py:325-403）。
  找不到 → `unconfirmed`（实际效果等于被过滤）。
- CodeQL 告警的函数名由 `_find_function()` 用 tree-sitter 按行定位（adapters.py:933-946）；
  Infer 告警直接用 report.json 的 `procedure`。

**CFG（z3_solver.py:674-995）**
- 只处理 `if/else`、`switch/case`、`return`、`goto/label`、表达式语句、声明。
- **没有任何循环处理**：`while_statement / for_statement / do_statement` 落到通用分支（z3_solver.py:832-852），
  逐个子节点调 `_process_node()`，而 `_process_node()` 对 `compound_statement` 返回 `None`
  （z3_solver.py:1024-1042）→ **循环体整体被丢弃**，循环里的分配、释放、return 全部不在 CFG 里。
  嵌套的裸 `{ ... }` 块同理被丢弃。
- `#if/#ifdef` 也没有处理（tree-sitter 的 `preproc_*` 节点被当作不认识的语句丢掉）。
- 事件节点：`ALLOC`（`x = f()` / `T *x = f()` 且 `f ∈ alloc_funcs`，或 `f ∈ alloc_out_params`）、
  `FREE`（`f(arg)` 且 `f ∈ free_funcs`，取第一个实参文本）、`ASSIGN`、`DEREF`（任何带指针实参的普通调用）、
  `RETURN`、`BRANCH`。
- `BRANCH` 节点虽然保存了 `condition` 文本，但 **编码中完全没有用到**（全文只有 z3_solver.py:860 把它拷贝进节点）。
  也就是说：没有 $b_v$，没有 "同一条件两次测试要一致" 的约束，分支纯粹是结构性的。

**编码（z3_solver.py:2361-2555）**
- 每条边一个布尔 `edge_{p}_{n}`，每个节点 `AtMost(incoming, 1)`，`reach[n] == Or(incoming)`，
  `edge → reach[pred]`；`allocd/freed/escaped` 三个状态位沿被选中的入边传播。这部分与论文公式 3/4/5 一致，
  和我们的 `PathEncoder`（symbolic.py:79-118）+ `add_state`（symbolic.py:121-139）结构相同。
- 额外约束：必须到达 `allocation_site` 所在行的某个节点、必须到达 trace 最后一行的 RETURN/EXIT 节点、
  trace 中间各行至少有节点可达（z3_solver.py:2402-2424）。行上没有节点时只打日志，不加约束。
- 目标：`Or(reach[r] ∧ allocd[r] ∧ ¬freed[r] ∧ ¬escaped[r])`，r 只取 `RETURN` 节点（不取人造 EXIT，2367）。
  没有 RETURN 节点（void 函数走到末尾）→ `"no exit/return nodes in CFG"` → **过滤**（2537-2538）。
- 追踪对象：`_choose_alloc_var()` 取 allocation 行上的 ALLOC 节点的变量名；否则取该行之前最近的 ALLOC；
  否则第一个有名字的 ALLOC。
- free 匹配：`free_funcs` 里的函数、且实参文本 == 跟踪变量或 trace 行上出现的 `tmp = var` 别名。
- escape：`return var;`（精确）、`*out = var`、`obj->f = var` / `obj.f = var`、
  以及 `ownership_sinks` 硬编码集合（`paste_set / cmdq_append / TAILQ_INSERT_*` 等 tmux 专用名，z3_solver.py:1970-1982）。
  注意 **写入全局变量**（`first_list = l`）和 **传给普通函数**（`list_append(l, p)`）都不算 escape。
- 与论文相反方向的额外过滤：分配结果写进 **参数的字段** (`dst->x = alloc()`) → 直接判 "ownership transferred to caller" 过滤（2380-2395）。

**"不可行"的所有来源（`is_feasible=False` → filtered）**
1. `_check_feasibility` 抛异常（CFG 构建或 Z3 出错）→ `"analysis failed"`（2201-2203）。
2. CFG 中没有 ALLOC 节点 → `"no allocation found"`（2372-2373）。
3. 找不到被跟踪变量名 → `"unknown allocated variable"`（2376）。
4. 分配写入参数字段（2380-2395）。
5. 没有 RETURN 节点（2537）。
6. 真正的 UNSAT：每条结构路径都 free/escape（2551）。

而 free 集合被 `_directly_frees_param_pointer()` 收紧（2019-2044，2087-2094）：只有函数体里字面出现
`free(arg) / g_free(arg) / kfree(arg) / zfree(arg) ...` 的包装器才算 FREE。对 Vim 来说
`vim_free` 算，`list_unref / dict_unref / clear_tv / free_tv` 等都不算。这一点让 UNSAT（来源 6）反而更难出现。

### 1.2 我们的实现（对照）

- CFG（cfg.py）：if/switch/goto/return 之外，**循环展开一次**、`#if` 当分支、向后 goto 丢弃 → 循环体内的分配/释放都在图里。
- 分支条件：每个条件文本一个布尔（symbolic.py:57-77），仅在变量单次定义时跨节点共享；常量条件直接取值。
- 状态：alloc/freed/escaped 三个位，`track()`（analysis.py:99-130）把 `p = 其它值`、null-check 的 F 边作为 clear；
  escape = return / 存入非局部左值（全局也算）。
- 函数定位按 **文件 + 行**（verify.py:27-49），不会串到同名函数。
- 找不到分配点 / 分配没绑定到局部变量 / CFG 失败 → **保留告警给 LLM**（analysis.py:343-371），
  官方是过滤。这是两边"保守方向"相反的核心差异。
- 每条告警一次判定，结果和理由写到 `z3_results.json`。

### 1.3 为什么官方过滤 91%、我们只有 21%

我们的 z3_results.json 理由分布（CodeQL + summaries，455 条）：318 条 "reaches exit unfreed"、
94 条 UNSAT、42 条 "allocation not bound to a local; kept"、1 条 "no recognised allocation; kept"。
官方实现在 **相同输入** 上会怎样，见 1.4 的实测。

结构性原因（读代码可以确定的）：
- **循环体丢弃**：Vim 大量分配在 `for/while` 里（`FOR_ALL_*` 宏展开也是 for）。官方 CFG 看不到这些分配 →
  "no allocation found" → 过滤。同样，释放在循环里也看不到，但这会让告警更"可行"，不会过滤。
- **void 函数无 RETURN 节点** → 过滤（我们的 exit 节点没有这个问题）。
- **分配写入参数字段** → 过滤。
- **函数按名查找** 串到另一文件的同名函数（Vim 里有大量 `static` 同名函数）→ 行号对不上 → 通常 "no allocation found" 或异常 → 过滤。
- **任何异常** → 过滤（我们是保留）。
- `#if` 混杂的函数（Vim 很常见）在官方 CFG 里会被静默截断。

这些都是"分析失败就丢掉"的策略，本质上不是论文公式 5 意义上的 UNSAT。论文正文把 Phase 5 描述为
过近似（只删结构上不可能的路径），但代码中大部分丢弃来自建模失败而不是 UNSAT。

### 1.4 实测：把官方 `WarningValidator` 直接跑在我们的告警上

脚本：`scratchpad/run_official_z3.py`（用官方 `CodeParser` 解析 Vim src、官方 `_parse_sarif` 读我们的
`results.sarif`、官方 `HintSet.from_json` 读我们的 `hints.json`，逐条调 `_check_feasibility`）。

注意事项：官方 `_get_all_alloc_funcs/_get_all_free_funcs` 的传递闭包在 10,463 个函数上 35 分钟没跑完，
实测时跳过了它，只用 BASE 集合 + hints（hints 本身已含 Stage 1 传递验证过的包装器；跳过只会让 ALLOC
节点更少、"no allocation found" 略多，方向是让官方过滤显得更激进而不是更宽松）。Z3 加了 20s 超时（没有触发）。

| 输入（我们的 Stage 2 告警） | 条数 | 官方 CONFIRMED | 官方 FILTERED | 官方 UNCONFIRMED（函数没找到） | 我们 feasible |
|---|---|---|---|---|---|
| CodeQL + summaries | 455 | 243 (53%) | 200 | 12 | 361 (79%) |
| CodeQL vanilla（无 hints） | 268 | 1 | 262 | 5 | 233 |
| Infer + summaries | 102 | 55 | 45 | 2 | 50 |

官方 FILTERED 的理由分布（CodeQL + summaries，200 条）：`no allocation found` 85、
`no exit/return nodes in CFG` 31、`analysis failed: None` 17、结构 UNSAT 约 57、
`unknown allocated variable` 6、`allocation written into parameter field` 4。
其中 `analysis failed: None` 是官方 CFG 构建的一个 bug：`return`/`goto` 之后同一块里还有语句时
`build_stmt()` 以 `in_id=None` 进入通用分支，`add_edge(None, nid)` 触发 `KeyError(None)`
（z3_solver.py:832-852），异常被当成"不可行"过滤。

两边判定的交叉表（CodeQL + summaries）：我们 feasible 且官方 CONFIRMED 204；我们 feasible 但官方过滤 147；
我们 UNSAT 但官方 CONFIRMED 39；两边都过滤 53。也就是说两个过滤器 **并不是包含关系**，官方的
"更严"主要来自建模缺口，我们的 UNSAT 来自条件变量和循环/`#if` 建模（官方没有）。

**对真 bug 的影响**：把官方过滤器套在我们最终的 42 条 CodeQL bug 上，会丢掉 12 条，其中：
- 上游已修复的 6 条（`expand_pattern_in_buf`、`ex_let_vars`、`f_getscriptinfo`、`u_read_undo`、`compile_dict`×2）；
- 我们人工确认、上游仍未修的 4 条：`f_setmatches`、`barline_parse`、`string_reduce`、`gui_gtk_draw_string`
  —— 前三条正是我们已经用 ASAN/gdb 验证并起草补丁的泄漏。
  `string_reduce` 被判 UNSAT 的原因很典型：泄漏的 `return` 在 `for` 循环体里，官方 CFG 丢掉整个循环体，
  剩下的唯一路径必经 `remove_funccal()`；`f_setmatches`/`barline_parse` 则是分配点在循环/`#ifdef` 里
  → "no allocation found"。
- vanilla CodeQL 的 268 条在官方过滤器下只剩 1 条（Vim 全部经 `alloc()/lalloc()` 包装，没有 hints 时
  CFG 里没有 ALLOC 节点），我们 vanilla 跑出的 31 条 bug（含 24 条真 bug）会全部被丢掉。
  论文 Table II 的 "vanilla CodeQL = 10" 显然不是这么算的（应是不经 Stage 3 的裸 CodeQL 结果）。

**结论**：官方代码在我们的 455 条告警上只过滤 44%，复现不出论文的 91%。剩下的差距只能来自
告警集合本身不同（论文的 1,011 条来自其增强查询 `ENHANCED_MEMORY_*.ql`，会在更多"没有可识别分配"的
函数上报告），而不是我们 Phase 5 编码有误。

---

## 2. Phase 6：LLM 验证

**官方（verify_bugs_llm.py）**
- 输入 `memory_safety_bugs.json`：按 **函数** 分组（pipeline.py:1389-1430），一个函数一次 LLM 调用，串行。
- 模型 `gemini-3.1-pro-preview`（run.sh `LLM_VERIFY_MODEL`）。
- prompt（`build_prompt()`，verify_bugs_llm.py:184-247）：项目/文件/函数、每条 issue 的行、message、allocation_site、
  trace 每步 ±2 行代码、bug 行 ±2 行代码；函数源码，bug 行加 `// <-- reported bug`；
  角色说明 + 决策策略 + JSON 输出 `{verdict, confidence, reason, bug_indices}`。
- 解析：`verdict` true/false/"TP"/"FP" → TP/FP，异常 → ERROR；`should_keep = verdict != FP`（ERROR 也保留）。
- 输出 `llm_verify_bugs.json`：`summary{tp, fp, error, should_keep}` + 按类别分的 tp/fp/error 列表。
  "#LLM-valid." 对应 `summary.tp`（**函数数**），"#bugs" 是作者人工/上游确认后的数，代码里没有。

**我们（verify.py:100-205）**
- 同样按 (file, function) 分组、一函数一次调用，prompt 文本基本逐句照搬官方（角色、决策策略、输出规则、
  `// <-- reported bug` 标记）；差异：trace 每步只给 message 不给 ±2 行代码，bug 行只给该行一行；
  用 gpt-5.6-luna，8 线程并发，磁盘缓存。
- `bugs.json` 按 `bug_indices` 展开成告警级条目；`n_llm_confirmed_functions` 对应官方 `summary.tp`。

结论：Phase 6 两边在语义上等价，我们的 prompt 上下文略少（trace 无代码片段）。

---

## 3. Infer 告警在 Stage 3 的处理

官方（adapters.py:1557-1593）：`allocation_site = bug_trace[0]` 的 file:line；`trace` = 全部 bug_trace 的 file:line；
`function_name = procedure`；file/line 用 report 顶层字段。Stage 3 之后 Infer 与 CodeQL **没有任何区别**，
同一个 `_check_leak_feasibility`。注意 Infer 的 bug_trace 会跨函数（进入被调函数），官方把这些行也作为
"必须可达"的中间约束（2418-2424），但行不在当前函数 CFG 里时只跳过。

我们（infer.py:64-93）：从 qualifier 解析出 `file:line:callee`，Phase 5 用 callee 名精确匹配分配调用；
其余逻辑与 CodeQL 相同。等价，且更精确一点。

---

## 4. 代码与论文不一致之处

1. **分支条件没有进入编码**：论文说每个分支引入 $b_v$、路径条件 $\varphi(\pi)$ 可满足才算可行；
   官方代码把 `condition` 存起来但从不使用，只有结构约束。我们实现了论文描述的版本（并处理了重定义变量的坑）。
2. **循环完全没建模**（论文说"函数内 CFG"，未提循环；但循环体整体消失不是"展开一次"，而是分析盲区）。
3. **大量过滤来自建模失败**（无分配节点、无 RETURN、异常、同名函数串号），论文把 Phase 5 描述成
   "只丢弃结构上不可行的路径" 的过近似过滤，这与代码行为不符；过滤率 91% 主要是这些来源。
4. **`ownership_sinks` 硬编码 tmux 函数名**，论文未提。
5. **"分配写入参数字段即所有权转移"** 规则，论文未提。
6. Infer 与 CodeQL 在 Stage 3 无差异化处理，这点与论文一致。

---

## 5. 结论

- 我们的 Stage 3 **在论文描述层面是正确的**，而且比官方代码更贴近论文（条件变量、循环展开、按文件+行定位）。
- 两边最大的行为差异在"建模失败时的默认动作"：官方 = 丢弃，我们 = 保留给 LLM。实测官方过滤器在
  我们的告警上过滤 44%（不是 91%），并且会丢掉我们 12 条真 bug（含 3 条已起草补丁的）。
  论文的 91% 无法用官方代码在我们的告警上复现，差距应来自告警集合（增强查询）而非我们的编码。
  不建议为了对齐数字去复刻官方的丢弃规则。
- Phase 6 等价；Infer 告警处理等价。
