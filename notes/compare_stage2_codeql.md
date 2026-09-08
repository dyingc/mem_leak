# Stage 2（CodeQL）：官方 MemHint 仓库 vs 我们的实现

官方代码：`tools/MemHint/src/analyzer/adapters.py`（`CodeQLAnalyzer`，1–1045 行）、`tools/MemHint/src/analyzer/queries/*.ql`、`tools/MemHint/src/core/pipeline.py` Phase 4（1040–1123 行）。
我们的代码：`memhint/analyzers/codeql.py`、`memhint/pipeline.py:120-158`（`Stage2`）。

## 结论先行

1. **摘要注入方式本质相同**：官方也是写 CodeQL data extension（`allocationFunctionModel` / `deallocationFunctionModel`），只是把 yml 直接写进 cpp-all 库包的 `ext/` 目录；我们用独立 model pack + `--model-packs`。两边的行列内容逐字段一致。
2. **"增强查询"在语义上等价于标准查询**：官方的两条 `*_enhanced.ql` 只是把标准查询的 `where` 按互斥的 reason/context 字符串分了类，结果集与标准 `MemoryNeverFreed.ql` / `MemoryMayNotBeFreed.ql` 相同（NeverFreed 在极端情况下可能多报重复行）。带 LLM 过滤谓词的 `*_filtered.ql` 默认**不生成**（`use_custom_queries=False`，`main.py` / `run.sh` 都没有打开）。所以我们只跑标准两条查询，与官方默认行为等价。
3. **告警计数方式相同**：两边都是 SARIF `runs[].results[]` 逐条计数，都不去重。我们 `REPRODUCTION.md:335,538` 里写的"对 (文件,行,规则) 做了去重"与代码不符（`parse_sarif` 没有去重；455 条里只有 410 个不同位置），这一句需要更正。
4. **1,011 vs 455 的差异不是查询数量或去重造成的**。剩下的可解释因素：(a) 摘要集合不同（论文 688 条验证摘要 vs 我们 493 条，Stage 1 的事）；(b) 数据库构建配置（官方 `make clean; make`，configure 自动检测 GUI 等，取决于机器）；(c) 官方运行时 `--download` 拉取的 cpp-queries 版本未固定。
5. **vanilla 基线一致**：官方靠构造参数 `skip_hint_injection=True`（`pipeline.py:57,544`，CLI 未暴露）跳过注入，其余不变；我们 `--vanilla` 同理（`pipeline.py:114-117`）。

判定：我们的 CodeQL 路径与官方实现**总体一致，可以认为"generally correct"**；不需要为了对齐官方而改代码，只需改文档。

---

## 1. 摘要如何注入 CodeQL

| | 官方 | 我们 |
|---|---|---|
| 机制 | data extension yml，直接写到 `<cpp-queries>/.codeql/libraries/codeql/cpp-all/<ver>/ext/allocation/hint.allocation.model.yml` 和 `ext/deallocation/hint.deallocation.model.yml`（`adapters.py:759-808`），分析完删除（`_cleanup_models`, 857-864） | data extension yml 放在独立 model pack `output/<proj>/codeql-ext/memhint-models/`，通过 `--additional-packs` + `--model-packs` 传入（`codeql.py:25-44, 63-75`） |
| allocator 行 | `["", "", False, name, "", "", "", True]`（`adapters.py:810-833`）；`arg_index` 被忽略，argN 类型 allocator 也按"返回值分配"建模 | `["", "", False, n, "", "", "", True]`（`codeql.py:33`），所有 Allocator 不分 target，同样处理 |
| deallocator 行 | `["", "", False, name, str(idx)]`，`idx = arg_index if >=0 else 0`（`adapters.py:835-851`，`models.py:148-159`） | `["", "", False, n, str(arg_index)]`（`codeql.py:30,35`）；我们 hints.json 的 Deallocator 全是 `argN`（116 个 arg0、37 个 arg1-7），无 None 情况 |
| 过滤 main | 跳过 `main/_main/""`（`adapters.py:780,790`） | 无此过滤（对结果无影响：Vim 没把 main 判为 allocator） |
| 校验 | `codeql resolve extensions` 打印 ✓/✗（`adapters.py:842-855`） | 无（但 455 vs 268 已证明 pack 生效） |

两边是同一个 CodeQL 扩展点、同样的列值；差别只是"pack 放哪"。

## 2. "增强查询"到底做了什么

官方 `MEMORY_QUERIES` 只有两条标准查询（`adapters.py:36-39`）。`use_enhanced_queries=True`（默认，`pipeline.py:58`，`main.py:71`）时，把两个硬编码 `.ql` 文本写到标准查询同目录下的 `MemoryNeverFreed_enhanced.ql` / `MemoryMayNotBeFreed_enhanced.ql`，并**只跑这两个文件**（`adapters.py:424-437, 505-520`）。

- `ENHANCED_MEMORY_MAY_NOT_BE_FREED.ql`：与 cpp-queries 1.5.8 的 `Critical/MemoryMayNotBeFreed.ql` `diff` 后，除注释外唯一差异是新增 `isErrorReturn(ret)` 并把 `where` 拆成 `isErrorReturn and context="error return" or not isErrorReturn and context="exit point"`（第 141-166 行）。两分支互斥且穷尽 ⇒ **结果集与标准查询完全相同**，只是 message 多了一个括号说明。
- `ENHANCED_MEMORY_NEVER_FREED.ql`：标准查询 `alloc.requiresDealloc() and not allocMayBeFreed(alloc)` 之上加了三段 reason（`not A and not B` / `A` / `B`，第 44-57 行）。`A`（在 if 分支里）和 `B`（循环内指针被覆盖）不互斥，同时成立时会**同一 alloc 报两行**；否则与标准相同。也就是说增强版只会等于或略多于标准结果，不会少。
- `*_FILTERED_TEMPLATE.ql`：仅多一句 `not leakFiltered(alloc)` / `not mayNotBeFreedFiltered(def)`，谓词体由 LLM 生成的"特殊函数"过滤片段拼接（`adapters.py:265-402`）。**触发条件是 `use_custom_queries=True`**（`pipeline.py:102, 962-978`），默认 False，`main.py` 和 `run.sh` 都没有传，`_prepare_enhanced_queries` 里 `qtext is None` 时直接跳过（`adapters.py:463-466`）。所以默认复现流程里根本没有 LLM 过滤查询。
- 增强查询不会把 deallocator 包装函数额外当作 free：free 语义全部来自 `MemoryFreed` 库里的 `DeallocationExpr`，即 data extension 注入的 `deallocationFunctionModel`（标准查询本来就支持间接 free，`freeCallOrIndirect`）。

对应我们：`codeql.py:21-22` 直接跑两条标准查询。**语义等价**。

## 3. 数据库构建与运行

| | 官方 | 我们 |
|---|---|---|
| 构建 | `proj_build_command.json`: `make clean`(可失败) → `codeql database create .codeql-db --source-root=<proj> --language=cpp --overwrite --command make`（`adapters.py:575-640`）；Vim 的 `make` 会自动跑 `./configure`，GUI 等特性取决于机器 | `codeql database create ... --command='make -j8'`（`build_dbs.log:10`），同样自动 configure，本机检测到 GTK（`-DFEAT_GUI_GTK`） |
| 分析 | `codeql database analyze db --format=sarif-latest --output=... --download <两个 enhanced .ql>`（`adapters.py:505-520`） | `codeql database analyze db <两条标准查询> --format=sarif-latest --output=... --threads=N --rerun`（`codeql.py:65-69`） |
| CodeQL 版本 | README 声明 2.23.9；cpp-queries 取 `~/.codeql/packages/codeql/cpp-queries/` 下最新目录（`adapters.py:481-503`），版本未固定 | 2.23.9 bundle，cpp-queries 1.5.8 / cpp-all 6.1.4 |

## 4. SARIF 解析与计数

- 官方 `_parse_sarif`（`adapters.py:870-914`）：每条 `results[]` 一个 `Warning`，取 `locations[0]` 的文件/行，函数名用 tree-sitter 按行定位（`_find_function`, 916-929），**无去重**。Phase 4 `#Warn.` = `len(warnings)`（`pipeline.py:1058`），只有 `source_root != project` 时才按目录过滤（1064-1080）。
- 我们 `parse_sarif`（`codeql.py:90-115`）：同样逐条 `results[]`，**无去重**；函数名在 Stage 3 用 `FunctionLocator` 定位。`n_warnings = len(warnings)`（`pipeline.py:153`）。

我们实际数据：`codeql` 455 条（may-not-be-freed 341 + never-freed 114），不同 (文件,行) 410 个；`codeql-vanilla` 268 条，全部不同位置。说明 CodeQL 对同一分配点、不同 return 语句会出多条结果，两边都原样计数。

**文档错误**：`REPRODUCTION.md:335`（"两条查询报同一 (文件,行) 只算一条"）和 `:538`（"我们对 (文件, 行, 规则) 做了去重"）与代码不符，应删除/更正；同时第 7 节把 1,011 vs 455 归因于"论文多跑重叠查询 + 我们去重"的推断也不成立——官方与我们跑的查询语义相同、计数方式相同。

## 5. vanilla 基线

官方：`Pipeline(skip_hint_injection=True)`（`pipeline.py:57, 98, 544-550`）→ `self.hints = HintSet()` 空 → `analyze()` 里 `if hints and hints.hints` 不成立，不写 yml（`adapters.py:140-143`），查询仍是两条 enhanced（= 标准）。CLI 没有暴露这个开关，论文 Table II 的 vanilla 数字应是作者直接调用得到的。
我们：`--vanilla` → `load_hints` 返回 `[]` → `pack=None`（`pipeline.py:114-117, 141`），跑同样两条标准查询。**一致**。

## 6. 与论文文字的出入（官方代码侧）

1. 论文说标准 2 条 + "3 类增强查询"；仓库里增强查询只是标准查询加分类字符串，第三类（LLM 过滤谓词）默认关闭且需要 `use_custom_queries`，`run.sh`/`main.py` 都没启用。按仓库默认复现，得到的就是标准查询结果。
2. 论文的 1,011 条告警无法由查询差异解释；仓库里也没有任何去重或额外查询能把 455 放大到 1,011。差异更可能来自摘要集合（688 vs 493 条）和构建配置。
3. 论文写 CodeQL 通过"model pack"注入，仓库实现是直接写入库包 `ext/` 目录——效果等价，不算矛盾。

## 7. 若要与官方逐字对齐，需要改什么

严格说不需要改代码；若追求形式一致：
- 可选：把 hint 中 `main/_main` 排除（`codeql.py:29-30`），零影响。
- 可选：Stage 2 就用 tree-sitter 填 `Warning.function`（官方在解析 SARIF 时就做了），我们放在 Stage 3，不影响计数。
- 必做：修正 `REPRODUCTION.md:335,538` 关于去重的描述，以及第 7 节"1,011 ≈ 2.2 × 455 是重叠查询"的推断。
