# Stage 1（摘要生成）对照：MemHint 官方实现 vs 我们的实现

官方代码：`tools/MemHint/`（`src/tree_sitter_parser.py`、`src/llm_client.py`、`src/symbolic/z3_solver.py`、`src/core/pipeline.py`）
我们的代码：`memhint/extract.py`、`memhint/summarize.py`、`memhint/analysis.py`、`memhint/pipeline.py`、`memhint/llm.py`

> 结论先行：Phase 1（抽取）和 Phase 2（LLM 提示词、批量、解析）与官方基本一致；Phase 3（Z3 校验）我们按论文 Eq.1/2 做了"真正的" CFG + Z3 路径/状态编码，而官方代码是一套启发式（正则 + 传递调用链 + 一个很宽松的 Z3 门）——官方对 Allocator 几乎"能过就过"，对 Deallocator 反而因一条 Redis 特化的正则在 Vim 上极其严格。因此 #Valid 的构成两边不同，但这是官方代码与论文描述的偏差，不是我们实现错误。

---

## 1. Phase 1 抽取 + 候选预过滤（#Extr. / #Cand.）

| 项目 | 官方 | 我们 | 影响 |
|---|---|---|---|
| 扫描目录 | `source_root` 全部递归（Vim 就是仓库根）`pipeline.py:314-322` | 同（`extract.py:255-260`，仅排除 `.git`） | 无 |
| 文件后缀 | `.c .cpp .cc .cxx .h .hpp .hxx .hh .inc .inl .def` `pipeline.py:314-318` | `.c .cpp .cc .cxx .hpp .hxx .h .hh` `extract.py:25-28` | Vim 无 .inc/.inl/.def，无影响 |
| `.h` 用哪个 parser | `.h` 用 **C++** parser `tree_sitter_parser.py:334-343` | `.h` 用 C parser `extract.py:35` | Vim 头文件是 C，C++ parser 偶尔会把 K&R/宏风格解析错；影响小 |
| 函数抽取 | `function_definition` 节点 `tree_sitter_parser.py:483-558` | 同 `extract.py:138-163` | 无 |
| `#if` 混入表达式导致整函数丢失 | 不处理 | `has_error` 时把条件编译行抹掉再解析一遍 `extract.py:212-252`（+73 个函数） | 我们多 ~73 |
| 宏 | **只转换 function-like 宏**（`if not macro_info.is_function_like: continue`）`pipeline.py:406-408` | function-like 宏 + **展开体中含调用的 object-like 宏** `extract.py:166-188` | 我们多 **627** 条 object-like 宏（`codebase.json` 统计） |
| 同名重复定义 | 保留代码更长者 `pipeline.py:333-337` | 先 `.c` 优先于 `.h`，再取更长 `extract.py:262-273` | 影响个别（Vim 的 `alloc` 在 dosinst.h 也有一份） |
| 指针 typedef 别名 | 全局先扫一遍 `tree_sitter_parser.py:66-140` | 每文件收集后合并 `extract.py:191-209` | 等价 |
| 预过滤 | `main/_main/wmain` 与名字含 `test` 的剔除；宏一律保留；其余要求签名含 `*` 或 typedef 指针别名 `llm_client.py:602-631`, `:684-693` | 同，额外把数组参数 `[` 也算指针 `extract.py:314-332` | 几乎一致 |

**#Extr. 差异估算**：我们 11,956 − 627（object-like 宏）= 11,329，再减去 `#if` 兜底重解析的 +73 ≈ 11,256，与论文 11,071 相差 ~1.7%（剩余来自 .h 优先规则、C vs C++ parser 的少量差别）。
**#Cand. 差异估算**：官方 8,539 / 11,071 = 77%；我们 9,318 / 11,956 = 78%。去掉 627 个 object-like 宏后我们是 8,691，与 8,539 只差 152。所以候选集口径一致，差异几乎全由 object-like 宏解释。

> 注：官方把 `#Valid` 定义为 **hint 条数**（`validated_hints_count = sum(len(hints))` `pipeline.py:982`），`#Summ.` 也是 hint 条数（`HintSet.add` 按 (type,target) 去重 `models.py:102`）。我们 `stage1_stats.json` 同样按条数统计（`n_summaries=1270`, `n_validated=493`），口径一致。

## 2. Phase 2 LLM 摘要生成（#Summ.）

| 项目 | 官方 | 我们 |
|---|---|---|
| 提示词正文 | `BATCH_HINT_GENERATION_PROMPT` `llm_client.py:122-210`（batch=20 时用这个；单函数用 `:54-120`） | `summarize.py:24-84` `INSTRUCTIONS`，语义分类、正负指标、四条 guideline、输出 schema 与官方**逐句相同** |
| 每函数块 | `### Function: name / Return type / Parameters / code / Context(called functions)` `llm_client.py:1262-1290` | `### Function i: name / Return type / Parameters / code / Context` `summarize.py:96-110` |
| callee 上下文 | 前 5 个 callee 的完整源码（`list(func.callees)[:5]`，**set 顺序随机**）`llm_client.py:1265` | 前 5 个（按名字排序）并裁剪到 80 行；主函数裁剪到 400 行 `summarize.py:18-20,103-107` |
| 批大小 | `HINT_BATCH_SIZE=20`（run.sh） | 20 |
| 输出格式 | 顶层按函数名分组 `{fn: {"hints":[...]}}` | 单个 `{"hints":[...]}`，name 字段区分 |
| 解析 | role∈{Allocator,Deallocator}；target 任意串，`arg_index` 从 `argN` 推出 `llm_client.py:1089-1130` | 同，另外**丢弃** `Allocator` 且 target≠`return` 的条目、以及 name 不在本批的条目 `summarize.py:117-135` |
| 并发 | 串行（tqdm 循环）`llm_client.py:967` | 8 线程 |
| 模型 | Gemini 3 Flash | gpt-5.6-luna（用户指定） |

**为什么官方 #Summ. = 2,539 而我们 1,270？**
1. 官方允许 `Allocator` 带 `argN`（out-parameter 型分配器，`_detect_allocator` 专门处理 `**` 参数 `z3_solver.py:1496-1506`）。我们在解析时直接丢弃 Allocator+argN（`summarize.py:127`）。论文只定义了 allocator→return，这是官方代码超出论文的部分。
2. 模型不同：Gemini 3 Flash 比 gpt-5.6-luna 更"积极"地标注（官方 27% 通过率也说明它给了很多假阳性摘要）。
3. 官方 callee 上下文不裁剪（更多上下文 → 更多"间接分配器"判断），我们裁剪到 80 行。
以上都不是实现错误，而是模型和保守解析的差异；**候选集本身几乎相同（见 §1）**。

## 3. Phase 3 Z3 摘要校验（#Valid.）——差异最大的一环

### 3.1 官方实际做法（与论文 Eq.1/2 的描述有明显出入）

**Allocator**（`HintValidator._validate_allocator` `z3_solver.py:1521-1538`）：
1. `_detect_allocator` `:1488-1519`：
   - 返回类型不含 `*` → 拒绝（**所有宏的 return_type 为空串，宏 Allocator 一律被拒**）。
   - 源码里子串含 `malloc(`/`calloc(`/`realloc(`/`strdup(`/`strndup(`/`g_malloc(`/`g_new(`/`kmalloc(` → 接受。
   - 传递链 `TransitiveAnalyzer.is_transitive_allocator` `:407-473`：沿调用图找到 BASE_ALLOCATORS，每层用正则要求 `return callee(` 或 `x = callee(...)`+`return x;`。
   - 否则只要代码里出现 `return` 字样 → **接受（"validated (weak)"）**。
2. Z3 门 `_z3_allocator_feasible` `:1810-1878`：CFG 无分配节点且无 `return alloc()` → **保留（conservative）**；有分配节点、精确流向追踪不到 → 只要分配节点可达就**保留**。真正被 Z3 拒绝的只有"分配节点存在但结构上不可达返回"的极少数。

→ 官方对 Allocator 的校验近似于"返回指针类型 + 有 return"即通过。

**Deallocator**（`_validate_deallocator` `:1540-1590`）：
1. `is_transitive_deallocator` `:475-673`：三种方法（调用图递归 + 参数映射；别名；正则模式）。
2. **`_frees_argument_itself` `:1627-1668`**：要求源码中字面出现 `\bfree(p)`/`cfree`/`sdsfree`/`decrRefCount`/`zfree`/`g_free`/`kfree`/`vfree`/`xfree` 且括号内恰好是参数名。这是 Redis 特化的列表。Vim 的包装器几乎全是 `vim_free(p)`，`\bfree` 在 `vim_free` 中因 `_f` 无词边界**不匹配**，所以 Vim 中凡是通过 `vim_free`/`VIM_CLEAR`/`free_xxx` 释放参数的函数都会被判为 "Does not free argN itself" 而**拒绝**。只有直接写 `free(p)` 的函数才可能通过。
3. 之后再过一个 Z3 可达性门（`_z3_deallocator_feasible` `:1895-1926`），同样以"无证据即保留"为原则。

**官方 CFG（`CFGBuilder.build` `:690-996`）不建模循环**：`while/for/do` 语句落到通用分支，只扫描语句的直接子节点，循环体内的分配/释放完全不可见；`if` 条件里的调用也不扫描。这也是它必须"无证据即保留"的原因。

### 3.2 我们的做法（`analysis.py`）
- 对每个摘要建 acyclic CFG（循环展开一次、goto 处理、`#if` 当分支）`cfg.py`，用 Z3 编码路径变量 + `alloc/freed/escaped` 状态（`symbolic.py`、`analysis.py:99-141`）。
- Allocator（Eq.1）`analysis.py:228-258`：存在可行路径，分配结果（含别名类）到达某个 `return` 且未被释放。
- Deallocator（Eq.2）`analysis.py:260-282`：参数（含别名类）在某条可行路径上被已知释放器释放。
- 传递性：被 LLM 标注但尚未校验的 callee 按需递归校验（深度 ≤10）`analysis.py:197-226`；宏按展开体做文本级匹配 `analysis.py:284-317`。
- 不做"无证据即保留"：找不到分配→返回路径就拒绝（414 条 Allocator 因此被拒，`hints_rejected.json`）。

### 3.3 通过率对比
- 官方：688/2539 = 27%。由 §3.1 可推断被拒的主要是：宏 Allocator（return_type 空）、非指针返回的 Allocator、以及 Vim 上几乎全部 Deallocator（`vim_free` 不匹配正则）。
- 我们：493/1270 = 39%（Allocator 340/764 = 45%，Deallocator 153/506 = 30%）。我们拒绝的 Allocator 多是"分配后存到字段/全局、或返回的不是分配结果"的（LLM 误标），这正是论文 Eq.1 想过滤的。

### 3.4 实证：把官方 HintValidator 直接跑在我们的 1,270 条原始摘要上
（脚本：`scratchpad/run_official_validator.py`，用我们的 `codebase.json` 构造官方 `FunctionInfo`）

结果（同一批 1,270 条原始摘要）：

| 校验器 | 通过总数 | Allocator | Deallocator |
|---|---|---|---|
| 官方 `HintValidator` | **572** | 567 | **5** |
| 我们 `SummaryValidator` | 493 | 340 | 153 |
| 两者都通过 | 324 | 321 | 3 |

官方通过的 Allocator 中 **360 条是 "validated (weak)"**（无任何分配证据，仅因返回指针 + 有 `return`），188 条靠传递链，19 条靠子串 `malloc(` 等；"weak" 通过的例子如 `BufferNew/BufferItem/BufferRepr`（if_py 里返回 `PyObject*` 的绑定函数）、`AddHeapType` 等——这些不是分配器，会直接进入 CodeQL 模型成为假分配源。
官方拒绝的原因分布：`Does not return pointer type` 197 条（**全部宏 Allocator**，如 `ALLOC_ONE`、`LALLOC_CLEAR_MULT`、`XDL_ALLOC_ARRAY` 都被拒）；Deallocator 共 501 条被拒，其中 253 条 "No deallocation call found"、248 条 "Does not free argN itself"（`vim_free(p)` 不匹配 `\bfree(`）。通过的 5 条 Deallocator 是直接调用 `free()`/`mch_free_acl` 这类。
→ 与 §3.1 的静态分析完全一致：**官方校验器在 Vim 上几乎不产生 Deallocator 摘要，并把大量非分配器当 Allocator 放行**；论文 Table IV 的 688 与我们的 493 之所以数字接近，构成却完全不同。

### 3.5 我们是否有不健全（unsound）之处
- 更严格的地方：Allocator 必须有"分配→返回"的可行路径。对 Vim 常见的 `p = alloc(); ... return p;` 没问题；但如果分配发生在我们 CFG 无法解析的位置（例如 tree-sitter 解析出错的函数体）会被拒；官方遇到这种情况会保留。这会让我们少一些真阳性摘要，但不会引入错误摘要。
- 更宽松的地方：Deallocator 只要求"某条可行路径释放了参数或其别名"（论文 Eq.2 原文即如此），不要求"必须是参数本身而非其字段"——但我们的别名类只含标识符级别名，`free(p->field)` 不会算作释放 `p`（`analysis.py:79-83` `_in_class` 只接受纯标识符），所以与官方 `_frees_argument_itself` 的意图一致，而且不依赖固定的释放函数名单。
- 我们接受 Allocator 只有 `target=return`；官方还有 out-parameter Allocator（`**` 参数）。论文正文与 Appendix 的 CodeQL 模型只用 `return` 形式，Infer 的 `--pulse-model-alloc-pattern` 也只支持返回值形式，所以这部分对 Stage 2 无影响。
- 共享条件布尔只对单定义变量生效（`symbolic.py` `_redefined_identifiers`），已在此前修复。

## 4. 官方代码与论文文本不一致的地方（Stage 1 范围）
1. 论文 Eq.1/2 描述的是 CFG 路径级 Z3 校验；官方 `_detect_allocator` 实际是"指针返回类型 + 子串/正则/传递链，否则 weak 通过"，Z3 只是一个几乎不拒绝的门。
2. 论文说 Z3 会"过滤不一致的摘要"；官方对 Deallocator 的核心过滤是 `_frees_argument_itself` 的固定名单正则（Redis 的 `sdsfree/zfree/decrRefCount`），与目标项目强相关，不是符号推理。
3. 官方 CFG 不建模循环（论文 III-C 说循环展开/无环化）。
4. 官方支持 out-parameter Allocator（`argN`），论文摘要格式只有 `return`。
5. 官方对 `#Extr.` 只算 function-like 宏；论文只说"函数与宏"。我们把展开为调用的 object-like 宏也算进去（多 627），这是我们对论文的较宽解读，可以在报告中注明。
6. 官方还有 Phase 2b "custom queries"（LLM 生成 CodeQL 查询）与 `use_llm_relabel`（Z3 拒绝后让 LLM 重标注），但 `main.py` 里两者都是默认关闭（`pipeline.py:59-60`），与论文流程一致。

## 5. 结论
- **Phase 1 / Phase 2：基本正确**，与官方一致；数字差异可由 object-like 宏（627）和模型差异解释。
- **Phase 3：我们按论文实现，官方按启发式实现**。两者会得到不同的 #Valid 构成：官方在 Vim 上会通过更多 Allocator、几乎不通过 Deallocator；我们相反地更严格地筛 Allocator、正常地通过 Deallocator。若想数字上贴近官方，可以（a）Allocator 加"无 CFG 证据即保留"的保守分支，（b）不丢弃 Allocator+argN；但那会偏离论文 Eq.1。建议保持现状并在 REPRODUCTION.md 中注明官方实现的这些偏差。
