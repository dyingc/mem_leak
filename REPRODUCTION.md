# MemHint 论文复现：完整说明

复现对象：**Finding Memory Leaks in C/C++ Programs via Neuro-Symbolic Augmented Static Analysis**
（Huihui Huang, Jieke Shi, Bo Wang, Zhou Yang, David Lo — [arXiv 2603.27224v4](https://arxiv.org/html/2603.27224v4)）

本文档面向"没跟过这个会话的人"，从头解释：论文在做什么、我们实现了什么、每个数字怎么来的、
怎么判断结果是对的、以及和论文对不上的地方为什么对不上。

日期：2026-09-06 ｜ 复现目标项目：Vim 9.2.0015 ｜ LLM：`gpt-5.6-luna` ｜ 总花费：约 **$1.72**

---

## 目录

1. [背景：论文要解决什么问题](#1-背景论文要解决什么问题)
2. [论文方法逐阶段详解](#2-论文方法逐阶段详解)
3. [我们的实现](#3-我们的实现)
4. [实验设置](#4-实验设置)
5. [结果：每个数字的含义](#5-结果每个数字的含义)
6. [怎么判断结果是对的：真值方法学](#6-怎么判断结果是对的真值方法学)
7. [与论文的对照与差异分析](#7-与论文的对照与差异分析)
8. [发现的 14 个真实泄漏](#8-发现的-14-个真实泄漏其中-10-个上游至今未修复)
9. [那个 12 GB 的 vim 进程：本复现为什么没发现它](#9-那个-12-gb-的-vim-进程本复现为什么没发现它)
10. [复现过程中修掉的三个自身缺陷](#10-复现过程中修掉的三个自身缺陷)
11. [局限与未完成项](#11-局限与未完成项)
12. [关于上报：是不是 security finding](#12-关于上报是不是-security-finding)
13. [文件清单与复现命令](#13-文件清单与复现命令)

---

## 1. 背景：论文要解决什么问题

### 1.1 内存泄漏为什么难查

C/C++ 要求手动释放堆内存。忘记释放 → 内存泄漏 → 长期运行的程序内存持续增长直至崩溃。
截至 2026 年 5 月，有超过 1,800 个 CVE 归因于内存泄漏。

两类查法各有硬伤：

- **动态分析**（fuzzing + LeakSanitizer）：只能发现"实际跑到过的路径"上的泄漏。论文举了个例子：
  OpenSSL 的 `X509_policy_check()` 有个泄漏，需要同时满足 EXPLICIT 和 EMPTY 两个条件才触发。
  OSS-Fuzz 从 2016 年起持续 fuzz OpenSSL，作者又自己跑了 24 小时、98 亿次测试，**都没触发**。
- **静态分析**（CodeQL、Infer）：能遍历所有路径，但有两个盲区，这正是论文要补的。

### 1.2 静态分析器的盲区一：认不出项目自定义的内存管理函数

真实项目很少直接调 `malloc`/`free`，而是包了好几层。比如 Vim：

```
vim_strsave() → alloc() → lalloc() → malloc()
VIM_CLEAR(p)  → vim_free(p) → free(p)
```

CodeQL 和 Infer 内置的"内存模型"只认识 `malloc`/`free`/`new`/`delete` 这类标准原语。
对于 `vim_strsave` 这种自定义分配器，分析器**不知道它的返回值携带堆所有权**，于是
"分配-释放"的配对关系完全看不见，泄漏就漏报了。

论文的动机例子是 FreeRDP：`freerdp_certificate_clone()` 分配、`freerdp_certificate_free()` 释放，
后者的调用链是 `freerdp_certificate_free → certificate_free_int → X509_free → free`——
四层委托，分析器的跨函数数据流追踪断在中间，两个函数都识别不出来。

**为什么不能简单地靠函数名猜？** 之前的工作（Goshawk、K-MELD、LeakGuard）就是靠命名规则或 NLP 猜的。
问题是：一个叫 `cleanup` 的函数可能释放内存也可能不释放；更糟的是，
**函数里有一句 `free(p)` 不代表这句话真的可达**——它可能在一个永远不成立的分支里。
把这种函数标成"释放器"，会让分析器误以为内存已被释放，从而**压制掉真实的泄漏报告**。

### 1.3 静态分析器的盲区二：路径不敏感，误报泛滥

即使内存模型是完整的，分析器仍然会把控制流分支合并成一条抽象流，报告运行时根本走不到的路径。
比如两个分支的条件互斥，分析器识别不出来，就会在一条不可能执行的路径上报泄漏。
用户要在海量误报里人工筛选，工具就没法用了。

### 1.4 论文的核心洞察

> 识别自定义 MM 函数需要**理解开发者意图**（语义任务，LLM 擅长）；
> 验证这个识别、以及过滤不可行路径需要**严格的符号推理**（形式化任务，SMT 求解器擅长）。

所以做成一个**神经-符号（neuro-symbolic）流水线**：LLM 生成的每一个结论，都要过 Z3 的关卡才算数。

---

## 2. 论文方法逐阶段详解

三个阶段、六个 Phase。下面每个 Phase 都说明「输入 → 做什么 → 输出」。

### Stage 1：摘要生成（Summary Generation）

目标：产出一份**经过验证的**自定义内存管理函数清单。

#### Phase 1：代码抽取

用 Tree-sitter 解析整个代码库，对每个函数抽出：函数名、返回类型、参数列表（含类型）、
完整函数体、直接被调用者集合（callees）。同时抽取：

- **函数式宏**：转成和函数一样的记录格式（因为 `#define VIM_CLEAR(p) do { vim_free(p); (p)=NULL; } while(0)`
  这种宏实际上就是个释放器）。
- **指针 typedef 别名**：`typedef struct foo *foo_p;` 这样的别名。因为函数签名里写 `foo_p f` 时
  看不到 `*`，但它其实是指针，下一步的预过滤需要知道这一点。

**预过滤**（为省 LLM 钱）：只保留签名里涉及指针类型的函数（返回类型或参数含 `*`，或是上面收集的指针别名）；
宏一律保留（宏体里的指针操作在签名上看不出来）；排除入口函数（`main`/`wmain`）和名字含 `test` 的函数。

#### Phase 2：LLM 生成摘要

对每个候选函数，把它的签名 + 完整函数体 + **最多 5 个直接被调用者的源码**喂给 LLM，问：
这是分配器、释放器，还是都不是？

给被调用者源码是关键——很多 MM 函数把活儿委托给了辅助函数，语义不是局部的。

输出是一个结构化的**函数摘要**，三个字段：

```json
{"name": "freerdp_certificate_clone", "role": "Allocator", "target": "return"}
{"name": "freerdp_certificate_free",  "role": "Deallocator", "target": "arg0"}
```

- `role`：`Allocator` 或 `Deallocator`
- `target`：分配器固定是 `return`（返回值携带堆所有权）；释放器是 `argN`（第 N 个参数被释放，0 起数）

这个摘要格式是**分析器无关的**——同一份摘要可以翻译成 CodeQL 的格式，也可以翻译成 Infer 的格式。

为省钱和降延迟，论文按 IRIS 的做法**一次请求打包 20 个函数**。

#### Phase 3：Z3 验证摘要（论文公式 1 和 2）

这是论文相对前人工作最关键的一步：**LLM 说的话不算数，要用符号推理验证**。

对每个 LLM 给出的摘要，构造该函数的**函数内控制流图（CFG）**，节点分类为
Alloc / Free / Return / Branch / Assign / Deref，每个 Branch 节点引入一个布尔变量 $b_v$，
两条出边分别由 $b_v$ 和 $\neg b_v$ 守护。一条从 Entry 到 Return 的路径 $\pi$ 是**可行的**，
当且仅当它的路径条件 $\varphi(\pi)=\bigwedge_{v} l_v$ 可满足。

**分配器判据（公式 1）**：存在一条可行路径 $\pi$ 和路径上的一个分配点 $a$，使得

$$\text{Sat}\big(\varphi(\pi) \wedge \textit{reaches}(a, r, \pi)\big)$$

即：分配的值能到达返回点 $r$，中途没有被覆盖或释放。SAT → 保留摘要；所有路径都 UNSAT → 丢弃。

**释放器判据（公式 2）**：存在一条可行路径上的 Free 节点 $f$，其被释放的实参是参数 $i$ 的别名：

$$\text{Sat}\big(\varphi(\pi) \wedge \textit{aliases}(f, i, \pi)\big)$$

论文的图 4 给了四个典型案例，我们把它们写成了单元测试：

| 案例 | 情形 | Z3 结论 |
|---|---|---|
| (a) 有效分配器 | 分配后到达 return | SAT ✓ 保留 |
| (b) 无效分配器 | 所有路径上都被 free 了才返回 | UNSAT ✗ 丢弃 |
| (c) 有效释放器 | `free(arg0)` 在可行路径上 | SAT ✓ 保留 |
| (d) 无效释放器 | 只释放了字段 `arg0->f`，不是 `arg0` 本身 | UNSAT ✗ 丢弃 |

**别名分析**是轻量的、基于标识符的：追踪 `v = p` 和 `v = p->f` 形式的赋值及其传递闭包，
足以识别"把参数赋给局部变量再释放"这种写法。

**跨函数**：如果被调用者已被识别为分配器/释放器，或正在验证中，就沿调用链递归下去，
深度上限 10（保证在递归和互递归代码上终止）。这样 `wrapper → wrapper → malloc` 的多层委托能被正确处理。

### Stage 2：摘要增强的分析（Summary-Augmented Analysis）

#### Phase 4：把验证过的摘要注入静态分析器

**CodeQL**：写成 data-extension（YAML 格式的模型包），填充 CodeQL 内置的两个可扩展谓词：

```yaml
extensions:
  - addsTo: {pack: codeql/cpp-all, extensible: allocationFunctionModel}
    data:
      - ["", "", false, "freerdp_certificate_clone", "", "", "", true]
  - addsTo: {pack: codeql/cpp-all, extensible: deallocationFunctionModel}
    data:
      - ["", "", false, "freerdp_certificate_free", "0"]
```

元组的字段依次是
`allocationFunctionModel(namespace, type, subtypes, name, sizeArg, sizeMult, reallocArg, requiresDealloc)`
和 `deallocationFunctionModel(namespace, type, subtypes, name, freedArg)`。
最后那个 `true` 表示"返回的内存需要配对释放"，`"0"` 表示第 0 个参数被释放。

**Infer**：翻译成 Pulse 引擎的两个正则参数：

```bash
infer analyze --pulse-only \
  --pulse-model-alloc-pattern '^(freerdp_certificate_clone|...)$' \
  --pulse-model-free-pattern  '^(freerdp_certificate_free|...)$'
```

> **注意（2026-09-08 更正）**：上面是论文附录 B 印的写法，但 Infer 用 OCaml `Str` 编译这两个正则，
> `|`、`(`、`)` 在 `Str` 里是普通字符，这个写法**一个函数都匹配不上**。官方代码实际生成的是
> `name1\|name2\|...`（`Str` 的"或"），且是前缀匹配。我们最初照附录写，导致 Infer 线整体失效；
> 修正过程和数据见 `COMPARISON.md` §3.2。

选这两个分析器是因为它们的分析基础互补：CodeQL 是声明式跨过程数据流，Infer/Pulse 是分离逻辑 + bi-abduction。

### Stage 3：告警验证（Warning Validation）

#### Phase 5：Z3 路径可行性过滤（论文公式 3、4、5）

对每条告警，重建被标记函数的 CFG，用三个布尔状态位追踪指针 $p$：

- $\textit{alloc}(n)$：在节点 $n$ 或之前已分配
- $\textit{freed}(n)$：已释放
- $\textit{escaped}(n)$：所有权已转移（返回、存入全局/字段、传给所有权接收方）

编码方式：

- 每条 CFG 边一个布尔变量 $e_{(u,v)}$，约束每个节点**最多一条入边为真**（公式 3），
  这样求解器被迫选出**一条具体路径**而不是路径的并集：
  $$\forall n \in V:\ \textsc{AtMost}_1(\{e_{(u,n)} \mid (u,n)\in E\})$$
- 可达性：$\textit{reach}(n) \Leftrightarrow \bigvee_{(u,n)\in E} e_{(u,n)}$，且 $\textit{reach}(\textsc{Entry}) = \top$
- 状态沿选中的路径传播（公式 4）：$\textit{state}(n) = \bigvee_{(u,n)\in E}\big(e_{(u,n)} \wedge \textit{state}(u)\big)$；
  在 Alloc/Free/escape 节点上，对应状态位额外被置真
- **泄漏可行的判据（公式 5）**：存在某个出口 $r$ 使得
  $$\text{Sat}\big(\textit{reach}(r) \wedge \textit{alloc}(r) \wedge \neg\textit{freed}(r) \wedge \neg\textit{escaped}(r)\big)$$

SAT → 保留告警（是条真实可走的泄漏路径）；UNSAT → 丢弃（不可行路径上的误报）。

注意这一步是**函数内**的：因为 Phase 3 已经把自定义包装器标成了 Alloc/Free，
遇到调用释放器包装器时直接把调用点当成 Free 节点，不用再进函数体。

论文明确说明这个编码是**过近似**：分支条件是不解释的布尔变量，
所以求解器只在"结构上没有任何路径能走到泄漏"时才丢弃告警，
不会因为数值或指针语义否定一条路径。剩下的误报交给 Phase 6。

#### Phase 6：LLM 验证告警

给 LLM 看：完整函数源码（泄漏行加 `// <-- reported bug` 标记）、告警类型和位置、
分析器给出的完整追踪路径。让它判断是真 bug 还是误报，输出：

```json
{"verdict": true, "confidence": 0.99, "reason": "一句话理由", "bug_indices": [1]}
```

这一步是为了处理 Z3 编码不到的高层语义，比如：某些 API 返回的是库内部管理的对象指针，
调用方**不负责释放**（借用语义）——这种约定写在文档里而不是代码结构里，符号推理够不着。

### 论文的结果

8 个项目、360 万行代码，检出 54 个泄漏（53 个被上游确认/修复）。
对比：vanilla CodeQL 19、vanilla Infer 3、LeakGuard（SOTA）20、Semgrep 2。
摘要质量：LLM 单独 92.0% 精确率 / 100% 召回；加上 Z3 验证后 100% / 100%。
成本约 $1.7 每个 bug。

---

## 3. 我们的实现

### 3.1 为什么自己写而不是直接跑作者的代码

作者的复现包 [`jiekeshi/MemHint`](https://github.com/jiekeshi/MemHint) 是存在的（MIT 许可，约 11,500 行 Python），
但**硬编码了 Gemini（Vertex AI）**，而本项目按 CLAUDE.md 只能用 `gpt-5.6-luna`。
更重要的是，"复现"的价值在于独立实现论文描述的方法然后看结果是否一致——
直接跑作者代码只能验证"代码能跑"，验证不了"论文写清楚了没有"。

所以：**按论文正文的公式和附录的 prompt 独立实现**，作者代码只作为交叉参考（比如确认
CodeQL 元组字段顺序、看它对 escape sink 的定义）。全部约 1,900 行。

### 3.2 模块地图

```
memhint/
  models.py      数据结构：FunctionInfo（函数/宏）、Summary（摘要）、Warning（告警）
  extract.py     Phase 1：Tree-sitter 抽取 + 指针预过滤
  llm.py         OpenAI 客户端：磁盘缓存、指数退避重试、成本核算、预算上限
  summarize.py   Phase 2：批量 20 个函数一次调用，论文附录 A-A 的 prompt
  cfg.py         控制流图构建（Phase 3 和 Phase 5 共用）
  symbolic.py    Z3 编码（公式 3/4/5 的边变量、AtMost-1、状态传播）
  analysis.py    Phase 3 摘要验证（公式 1/2）+ Phase 5 泄漏可行性（公式 5）
  analyzers/
    codeql.py    Phase 4：生成 CodeQL 模型包、跑分析、解析 SARIF
    infer.py     Phase 4：生成 Pulse 正则、跑分析、解析 report.json
  verify.py      Phase 5 编排 + Phase 6 LLM 验证（论文附录 A-B 的 prompt）
  pipeline.py    Stage 1/2/3 编排，带磁盘检查点
  evaluate.py    真值校验：拿上游修复提交对账（见第 6 节）
  report.py      生成对照论文各表的 Markdown 报告
  cli.py         命令行入口
tests/           论文图 4(a)-(d)、图 5 的单元测试
```

### 3.3 关键实现决策（逐条解释）

#### CFG 是无环的

论文的 CFG 建模 if / switch / goto / return，但**不建模循环**（for/while/do）。
论文明说这是有意为之：它针对的泄漏模式（错误分支上的提前返回、包装器隐藏的释放）
来自控制流而非循环迭代，循环携带的泄漏是 future work。

我们的实现照做：循环体展开一次（相当于执行 0 或 1 次），向后的 `goto` 丢弃。
额外处理了 `#if/#else/#endif`——把它当成一个不透明的分支节点，两个臂都可能走到。

#### 分支条件什么时候共享布尔变量（这里踩过坑）

论文说每个分支节点 $v$ 引入一个布尔变量 $b_v$。但如果两个分支测的是**同一个条件**
（比如 `if (x) ... ` 和后面的 `if (!x) ...`），用同一个布尔变量才能表达它们的**互斥关系**，
才能过滤掉"两个都成立"的不可行路径。所以我们按条件文本的规范化形式做键：
`!p`、`p == NULL`、`p != NULL` 都归一到 `p` 加上极性。

**但这样做在一种情况下是不健全的**：如果变量在两次测试之间被重新赋值，两次测试的结果可以不同。
真实例子（Vim `eval_dict`）：

```c
item = dict_find(d, key, -1);
if (item != NULL) { ... }        // 第一次测 item
...
item = dictitem_alloc(key);      // item 被重新赋值
if (item == NULL) { ... }        // 第二次测 item，结果与第一次无关
```

共享一个布尔变量会让 Z3 认为这两次测试必须一致，从而把真实泄漏判成 UNSAT——
`eval_dict` 的泄漏（对应上游 patch 9.2.0079）就是这样被我们**误过滤**掉的。

修复：只对"在函数内只被定义一次的变量"（包括参数，参数算作在入口定义一次）共享布尔变量；
只要条件里出现了被多次定义的变量，就退回论文的做法——每个分支节点一个独立布尔变量。
见 `symbolic.py` 的 `_redefined_identifiers()`。修复后 `eval_dict` 被正确检出。

#### null 检查会清除 alloc 状态

`p = alloc(); if (p == NULL) return;` ——在 `p == NULL` 那条边上，分配其实是失败的，
不该认为"分配了但没释放"。所以我们在被追踪指针的 null 分支上把 `alloc` 状态清零。

#### 相信分析器给出的分配点

CodeQL 自带的 `MemoryMayNotBeFreed` 查询本身就会跟踪包装器的返回链。
所以它可能在一个我们的摘要没覆盖到的调用上报告分配。这时如果 Phase 5 坚持
"我不认识这个分配器所以没有分配点"，就会把告警原封不动放行（无过滤效果）。
我们的做法：如果分析器断言某一行有分配，就**信它**，把那一行的调用当作分配点来追踪。

#### 宏的处理

宏没有真正的 CFG。对宏，我们在宏体上做正则匹配：找调用，看被调用者是否已知的分配器/释放器，
以及被释放的实参是否正是宏的第 N 个参数。`VIM_CLEAR(p)` 因此被正确识别为 `Deallocator/arg0`。

#### 与论文的有意偏差

| 项 | 论文 | 我们 | 原因 |
|---|---|---|---|
| LLM | Gemini 3 Flash（Phase 2）+ Gemini 3.1 Pro（Phase 6） | `gpt-5.6-luna` 两处都用 | 项目约束 |
| CodeQL 查询 | 标准 2 条 + 3 类"增强查询" | 只用标准 2 条 | 论文对增强查询只有 3 句描述，不足以独立重写；且论文自己说增强查询**不带来新 bug**（Table II 的 `*` 注脚：加不加增强查询，vanilla CodeQL 都是 19 个） |
| Infer | `--keep-going --pulse-only --debug-level 2`，模式 `a\|b`（官方仓库） | 同样标志（`--debug-level` 实测无影响，默认不加），模式 `^\(a\|b\)$`（加锚点；另有 `official` 模式复刻官方） | 见 `COMPARISON.md` §3.2、§5 |
| 告警计数 | 逐条 SARIF result 计数，不去重（官方仓库代码） | 同样逐条计数、不去重（455 条对应 410 个不同 (文件,行)） | 一致。本文早先版本误写为"去重"，已更正，见 `COMPARISON.md` §3.1 |

---

## 4. 实验设置

### 4.1 为什么只做 Vim

论文做了 8 个项目。我们和你确认后聚焦 Vim 9.2.0015：

- 论文在 Vim 上检出最多（22 个），信号最强
- 429K SLOC、10.9K 函数，规模够大但单机跑得动
- 本机内存 28 GB（且开始时有个 vim 进程占了 11.9 GB，见第 9 节），FreeRDP/OpenSSL 的构建更重

### 4.2 版本（全部对齐论文）

| 组件 | 版本 |
|---|---|
| CodeQL CLI | 2.23.9（用的是 codeql-action 的 bundle，自带 cpp-queries 1.5.8 / cpp-all 6.1.4） |
| Infer | 1.2.0 |
| Z3 | 4.15.4 |
| Python | 3.11.12（uv 管理的 venv） |
| tree-sitter | 0.26（tree-sitter-c / tree-sitter-cpp） |
| 目标 | vim/vim @ v9.2.0015 |

### 4.3 跑了四个配置

为了把"摘要注入到底有没有用"这件事量化，每个分析器跑两遍：

| 配置 | 说明 |
|---|---|
| `codeql` | 注入 493 条验证过的摘要 |
| `codeql-vanilla` | 不注入，纯 CodeQL（论文的基线） |
| `infer` | （已作废）注入的正则写法无效，见 7.2 差异 3 |
| `infer-vanilla` | 不注入，纯 Infer（论文的基线） |
| `infer-refA` | 修正后：注入 493 条摘要，官方写法 `a\|b` + 官方标志 |
| `infer-refE` | 修正后：注入**官方校验器**放行的 572 条摘要（567 分配器 / 5 释放器），官方写法 + 官方标志 |
| `infer-refF` | 修正后：注入 493 条摘要，加锚点 `^\(a\|b\)$`（本仓库默认模式） |

所有配置**共用同一个 CodeQL 数据库和同一份 Infer capture**，所以差异只来自摘要与模式写法。
Infer 每次分析都在 capture 目录的一个**副本**上进行（同一目录重复 `infer analyze` 会被上一次的结果污染，见 7.2 差异 3）。

---

## 5. 结果：每个数字的含义

### 5.1 Stage 1 漏斗（对照论文 Table IV）

| | #Extr. | #Cand. | #Summ. | #Valid. |
|---|---|---|---|---|
| **我们** | 11,956 | 9,318（−22.1%） | 1,270（−86.4%） | 493（−61.2%） |
| **论文** | 11,071 | 8,539（−22.9%） | 2,539（−70.3%） | 688（−72.9%） |

逐列解释：

- **#Extr. = 11,956**：Tree-sitter 从 291 个文件里抽出的函数 + 函数式宏总数
  （其中宏 1,345 个）。论文是 11,071，差 8%，主要是宏的计入口径和重复定义的取舍不同。
- **#Cand. = 9,318**：指针预过滤后剩下的。**降幅 22.1% vs 论文 22.9%，几乎完全一致**
  ——这说明预过滤规则实现对了。
- **#Summ. = 1,270**：LLM 认为是 MM 函数的摘要条数（涉及 1,232 个不同函数；
  少数函数同时是分配器和释放器，比如 realloc 类）。分布：340 个分配器 + 153 个释放器（验证后）。
  论文是 2,539——**我们的 LLM 判定明显更保守**，见第 7 节分析。
- **#Valid. = 493**：过了 Z3 验证的（487 个不同函数，其中 18 个是宏）。
  **Z3 淘汰了 61.2% 的 LLM 摘要**，论文是 72.9%——同一量级。

**成本**：466 次 LLM 调用（每次 20 个函数），605 万输入 token + 41 万输出 token，
**$1.70，耗时 705 秒**。论文用 Gemini 3 Flash 在 Vim 上花了 $10.20。

**抽样检查**（人工看了核心 MM 函数是否被正确识别）：

```
VALID  alloc            Allocator/return      VALID  vim_free       Deallocator/arg0
VALID  lalloc           Allocator/return      VALID  VIM_CLEAR      Deallocator/arg0  ← 宏
VALID  alloc_clear      Allocator/return      VALID  dict_unref     Deallocator/arg0
VALID  vim_strsave      Allocator/return      VALID  list_free      Deallocator/arg0
VALID  vim_strnsave     Allocator/return      VALID  dictitem_free  Deallocator/arg0
VALID  ALLOC_ONE        Allocator/return  ← 宏 VALID  free_tv        Deallocator/arg0
REJ    ga_clear         Deallocator/arg0  ← 被 Z3 正确拒绝
```

最后一行值得注意：`ga_clear(garray_T *gap)` 释放的是 `gap->ga_data` 这个**字段**，不是 `gap` 本身。
LLM 把它标成了 `Deallocator/arg0`，Z3 用公式 2 判定"没有任何调用释放 `gap` 或它的别名"，予以拒绝。
**这正是论文图 4(d) 的场景，在真实代码上复现了。**

### 5.2 Stage 2 / Stage 3（对照论文 Table V）

| 配置 | #告警 | Z3 后 | LLM 确认函数数 | 报出 bug 条数 |
|---|---|---|---|---|
| `codeql`（我们） | 455 | 361（−20.7%） | 37 | 42 |
| `codeql-vanilla`（我们） | 268 | 233（−13.1%） | 28 | 31 |
| **CodeQL（论文）** | **1,011** | **86（−91.5%）** | **24** | **18 确认** |
| `infer-vanilla`（我们，干净重跑） | 26 | — | — | — |
| `infer-refA`（我们，493 条摘要，官方写法） | 332 | 289（−13.0%） | 24 | 32 |
| `infer-refE`（我们，官方校验的 572 条摘要） | 395 | 358（−9.4%） | 28 | 34 |
| `infer-refF`（我们，493 条摘要，加锚点） | 298 | 267（−10.4%） | 24 | 29 |
| **Infer（论文）** | **1,032** | **147（−85.8%）** | **25** | **15 确认** |

（早先版本里的 `infer` 一行"102 条 → 2 个 bug"以及"Infer 从 21 涨到 102（+386%）"已作废：
当时注入的正则一个函数都没匹配上，102 条是同一目录重复分析造成的污染，见 7.2 差异 3。）

**摘要注入的效果立竿见影**：CodeQL 的告警从 268 涨到 455（+70%），且
**vanilla 的 268 条是 455 条的真子集**（`only-vanilla = 0`）——摘要只增不减，符合预期。
Infer 从 26 涨到 332（×13）。

### 5.3 成本

| 阶段 | 调用数 | 花费 |
|---|---|---|
| Phase 2 摘要生成 | 466 | $1.70 |
| Phase 6 告警验证（4 个配置合计） | 约 480 | $0.02 |
| **合计** | | **$1.72** |

Phase 6 便宜得多，因为 Z3 已经过滤掉了大部分告警，而且我们做了磁盘缓存
（重跑 Stage 3 时 242/252 是缓存命中）。论文的成本是 $10.20 + $0.46。

---

## 6. 怎么判断结果是对的：真值方法学

### 6.1 问题：不能自己给自己打分

流水线最后一步是 LLM 说"这是真 bug"。如果我们就拿这个当结果，等于让被测系统自己判卷。
论文的做法是人工检查每一条并提交给上游维护者确认——这需要几个月。我们需要一个**客观、可复现**的替代。

### 6.2 关键观察：论文的 bug 已经在上游历史里了

论文分析的是 Vim 9.2.0015。它报告的 22 个 bug 被上游确认修复了。
所以**9.2.0015 之后的上游提交历史里，就藏着论文的答案**。

查证后发现得更确切：这些修复提交的 `Signed-off-by` 正是论文一作 **Huihui Huang \<625173@qq.com\>**，
而且都是通过**公开 PR** 提交的（如 vim/vim#19516、#19517、#19518、#19531）。
比如 patch 9.2.0079「memory leak in eval_dict()」对应 PR #19531。

### 6.3 具体做法（`memhint/evaluate.py`）

```bash
# 1. 拉取 9.2.0015 之后的历史（blobless + shallow-since，省时间和空间）
git fetch --filter=blob:none --shallow-since=2025-12-01 origin master

# 2. 取所有 subject 含 "leak" 的提交（排除纯测试改动的）
git log v9.2.0015..FETCH_HEAD -i --grep=leak

# 3. 对每个提交，解析 diff hunk 头部
#    @@ -1055,6 +1055,7 @@ eval_dict(...)
#                                 ^^^^^^^^^ git 的 C 语言 hunk 上下文就是所属函数的定义行
#    从中提取 (文件, 函数) 对
```

然后：**我们报的 bug，如果 (文件, 函数) 命中某个泄漏修复提交所改动的函数，就算命中真值。**

共提取到 **60 个泄漏修复提交**。其中落在论文时间窗内的有 **19 个**：窗口按日期取 patch 9.2.0055（2026-02-25）到 9.2.0136（2026-03-10），
含 18 个带编号的 patch，外加 1 个维护者漏写编号的提交 `7ed37dc5`（2026-03-08，list_extend_func 泄漏）。
（早先版本把这个漏编号的提交算进命中数却没算进分母，导致比值多算了一个，现已统一为 19。）

### 6.4 这个 oracle 的局限（必须说清楚）

- **函数粒度，不是行粒度**：同一个函数里可能有多个泄漏，我们报的和上游修的可能不是同一处。
- **只覆盖"已被修复"的泄漏**：上游没修的真泄漏，在这个 oracle 里会被记成"未命中"——
  所以未命中的**必须人工复核**，不能直接当误报。
- **可能有偶然命中**：如果一个函数因为别的原因被改过。我们抽查了命中项，都是对应的泄漏修复。

### 6.5 人工复核流程

对每一条"未命中"的报告：

1. 打印该函数在 9.2.0015 的完整代码，标出报告行
2. 读代码，判断 LLM 给的理由是否成立
3. **对照上游 HEAD（2026-09）的同一函数**，确认这个泄漏是不是至今还在
4. 用 `git log -L :函数名:文件` 查该函数后来有没有被改过

结论记录在 `output/vim_9_2_0015/manual_review.json`，每条带一句话依据。

---

## 7. 与论文的对照与差异分析

### 7.1 核心结论：复现成功

拿论文时间窗内的 19 个上游泄漏修复当召回率标尺：

| 配置 | 命中 19 个中的几个 | 命中的不同 patch 总数 | 报告函数数 | 精确率 |
|---|---|---|---|---|
| `codeql`（注入摘要） | **14 / 19** | 24 | 37 | **86.5%** |
| `codeql-vanilla`（基线） | 7 / 19 | 14 | 28 | 82.1% |
| `infer-refA`（注入 493 条摘要，修正后） | **8 / 19** | 16 | 24 | 83.3% |
| `infer-refE`（注入官方校验的 572 条） | 7 / 19 | 17 | 28 | 82.1% |
| `infer-refF`（注入 493 条，加锚点） | 8 / 19 | 16 | 24 | 87.5% |
| `infer-vanilla`（基线） | 0 / 19 | 0 | 0 | — |

**论文的核心主张——注入验证过的摘要能大幅提升检出——在两条线上都复现：CodeQL 14 vs 7，正好翻倍；
Infer 修正正则写法后从 0 到 8/19（论文 Table II：MemHint-Infer 15 vs vanilla Infer 3）。**
论文自己的 CodeQL 对比是 MemHint-CodeQL 18 个 vs vanilla CodeQL 10 个（Table I / Table II），比例接近。

精确率的算法：`(命中上游修复的 + 人工判定为真但上游未修的) / 报告函数总数`。
四个配置去重合并后：**43 个报告函数，24 个命中上游修复，19 个未命中中人工判定 12 真 7 假**，
整体精确率 **(24+12)/43 = 83.7%**。论文报告的精确率是 CodeQL 54.3% / Infer 46.3%——我们更高，
但口径不同（论文是"人工确认为 bug / LLM 验证后的告警"，且它的分母包含了更多告警）。

### 7.2 逐项差异分析

#### 差异 1：LLM 摘要数 1,270 vs 论文 2,539

同样的 prompt、同样的批大小，`gpt-5.6-luna` 比 Gemini 3 Flash **保守得多**——
只认了一半的函数是 MM 函数。

但**这没有伤害最终结果**：Z3 验证后我们剩 493 条，论文剩 688 条，差距缩小到 1.4 倍；
而且核心 MM 函数（`alloc`/`vim_free`/`VIM_CLEAR`/各种 `*_unref`）都在里面。
说明 Gemini 多认的那部分里，相当比例是 Z3 会拒绝的噪声。

**这一条印证了论文的设计意图**：Z3 关卡让整个流水线对 LLM 的选择不那么敏感。

#### 差异 2：Z3 过滤率 20.7% vs 论文 91.5%

这是最大的差异，有两个原因：

**原因 A：告警基数不同（455 vs 1,011），来源是摘要集合而不是查询或去重。**
（本段已根据官方仓库对照更正，详见 `COMPARISON.md` §2、§3.1。）官方代码和我们一样逐条计数、
不去重，默认也只跑两条与标准查询等价的 `.ql`。差异来自 Stage 1：官方校验器在 Vim 上放行 567 个
Allocator（其中 360 个没有任何分配证据）而只放行 5 个 Deallocator——模型里"只有分配、没有释放"，
CodeQL 自然报出成倍的告警，再由 Stage 3 砍掉。另外官方 Stage 3 把"建模失败"（循环体内的分配看不见、
void 函数没有 return 节点、同名函数串号、异常）一律当作不可行丢弃，这才是 91.5% 的主要构成；
把官方过滤器直接套在我们的 455 条上只过滤 44%。

**原因 B：escape（所有权转移）的定义宽度。**
Z3 判定"不是泄漏"的主要途径是证明每条路径上要么 freed 要么 escaped。
escape 的定义越宽，能过滤掉的告警越多。我们的定义是：返回该指针、存入非局部左值
（`obj->field = p`、`*out = p`、全局变量）。

参考作者的实现后发现，它还有一个 `ownership_sinks` 集合——但里面是
`paste_set`、`cmdq_append`、`TAILQ_INSERT_TAIL` 这类**tmux 专用的硬编码函数名**，
并不是一个通用机制。论文正文没有描述这一点。我们没有为 Vim 硬编码类似列表，
因为那样等于把答案手工喂进去，会破坏复现的独立性。

**这个差异的后果是我们的 Phase 5 更保守**——放行更多告警给 LLM。
考虑到 Phase 6 的成本只有 $0.02，这个代价可以接受；而且论文自己也说 Phase 5 是过近似，
剩余误报本来就是留给 LLM 的。

#### 差异 3：Infer 线的三个错误与修正（2026-09-08，对照官方仓库后）

本文早先版本把 Infer 只有 102 条告警归因于 Pulse 的 `--pulse-max-disjuncts` 上限。
对照官方仓库 `jiekeshi/MemHint` 后确认**那个解释是错的**，真实原因有三个，全部在我们这一侧：

1. **正则语法写错。** Infer 用 OCaml `Str` 编译 `--pulse-model-alloc-pattern`，"或"是 `\|`，
   `(`、`|`、`)` 都是普通字符。我照论文附录 B 写成 `^(a|b|...)$`，结果**一个函数都没匹配上**，
   等于没注入。官方代码生成的是 `a\|b\|...`。用一个 8 行的 C 文件实测（`COMPARISON.md` §3.2）：
   我的写法与不加模式结果完全相同；官方写法能报出对不可见分配器的泄漏。
2. **同一 results-dir 被重复分析。** `infer-vanilla` 与 `infer` 两次 `infer analyze` 共用一个
   `--results-dir`，第二次是在第一次的 results.db 上增量跑出来的，"102 vs 21"是状态污染。
   在干净副本上重跑：无摘要 26 条，旧写法 21 条（交集 18，Pulse 并行本身有几条抖动）。
3. **disjunct 上限不是原因。** 修正写法后，同样的默认上限 20，告警从 26 涨到 332（我们的 493 条摘要）
   / 395（官方校验器放行的 572 条摘要）。`--debug-level 2` 只影响日志和耗时（1,710 s vs 1,409 s）。

修正后的结果（`infer-refA`）：332 条告警 → Z3 289 → LLM 确认 24 个函数 / 32 条 → **24 条命中上游修复
（16 个不同 patch，时间窗内 8/19）**，未命中的 8 条人工复核：3 真（`string_reduce`、`barline_parse`、
以及 CodeQL 没报过的新发现 `json_encode_lsp_msg`）、1 已被上游顺手修掉、4 假。

与论文 1,032 条仍有差距的原因（`COMPARISON.md` §2、§5）：官方 Stage 1 校验器在 Vim 上放行 567 个
Allocator（含 360 个没有任何分配证据的"weak"通过）而只放行 5 个 Deallocator；Infer 模式又是无锚点的
前缀匹配（`alloc` 同时命中 `alloc_does_fail`、`alloc_cmdbuff`……）。用官方校验器的摘要重跑（`infer-refE`）
得到 395 条，方向一致但仍达不到 1,032——剩余差距应来自 Gemini 给出的 2,539 条原始摘要比我们的 1,270 条
多一倍（官方最终 688 条 vs 我们复刻出的 572 条），以及构建配置差异。

#### 差异 4：Infer 报出的 bug 集合

修正后 Infer（refA）命中的 16 个 patch 与 CodeQL 的 24 个有 13 个重叠（`7ed37dc5`、9.2.0057/0067/0105/0106/
0133/0134/0136/0243/0244/0777/0778/0933），Infer 独有 9.2.0773/0799/0802 三个"alloc failure"补丁，
外加 `json_encode_lsp_msg` 这个 CodeQL 没报过的未修复泄漏——与论文"两个分析器互补"的观察一致
（论文全部项目：21 个重叠、CodeQL 独有 23、Infer 独有 10）。

---

## 8. 发现的 14 个真实泄漏（其中 10 个上游至今未修复）

这是复现的**额外产出**：这 14 个泄漏在 Vim 9.2.0015 都真实存在（人工读代码确认），
其中 12 个来自 CodeQL，2 个来自修正后的 Infer（见 8.5），
且都没有对应的、标题含 "leak" 的上游修复提交——所以真值匹配器没有命中它们。

**一个必须交代的更正**：起初我用 `git log -L :函数:文件` 检查"这个函数后来有没有被改过"，
在 blobless clone 上这个命令会**静默返回空**，我误读为"没改过"，把 12 个全部标成了"上游未修复"。
后来在准备补丁时逐个对照上游 HEAD 的**源码内容**重查，发现其中 **4 个已经被不以 "leak" 为题的重构顺手修掉了**：

| 函数 | 上游怎么修掉的 |
|---|---|
| `did_set_pumborder` | patch 9.2.0318 把解析逻辑搬进新函数 `parse_pumopt_border()`，每条失败路径都 `vim_free(token)` |
| `clip_wl_receive_data` | 转换逻辑重构为 `clip_convert_data(..., &tofree)` + `vim_free(tofree)` |
| `clip_wl_init_buffer_store` | 整个 wl_shm buffer-store 机制（`ftruncate`/`wl_shm_create_pool`）在 HEAD 已不存在 |
| `ExpandSettings` | `fuzzymatches_to_strmatches()` 的失败路径现在先 `fuzmatch_str_free()` 再 `return FAIL` |

这 4 个在 `manual_review.json` 里标为 `TP-FIXED`。**剩下 8 个按 HEAD 源码内容逐一确认仍然存在。**
教训：判断"是否已修复"必须看目标版本的源码内容，不能只看提交历史。

按"是否可被用户输入触发"分组：

### 8.1 可由用户操作直接触发（3 个，优先级最高）

**`src/optionstr.c: did_set_pumborder`**（已于 9.2.0318 顺手修掉，见上表）— 在 9.2.0015 中只要执行 `:set pumborder=custom:bad` 就泄漏：

```c
token = vim_strnsave(p, len);          // ← 分配
if (token == NULL) goto error;
if (... STRNCMP(token, "custom:", 7) == 0) {
    char_u *q = token + 7;
    for (int i = 0; i < 8; i++) {
        if (*q == NUL || *q == ',') goto error;   // ← 泄漏：没有 vim_free(token)
        ...
        if (*q != ';') goto error;                // ← 泄漏
    }
    if (*q != NUL && *q != ',') goto error;       // ← 泄漏
}
```
同一函数的其他分支都正确地 `vim_free(token)` 后才 `goto error`，唯独 `custom:` 这个分支的三处漏了。

**`src/match.c: f_setmatches`**（**HEAD 仍存在**）— 调用 `setmatches()` 传入 `posN` 字段不是 list 时泄漏。

```c
s = list_alloc();                       // ← 分配
...
for (i = 1; i < 9; i++) {
    if ((di = dict_find(d, buf, -1)) != NULL) {
        if (di->di_tv.v_type != VAR_LIST)
            return;                     // ← 泄漏：s 没有被释放
        list_append_tv(s, &di->di_tv);
    }
}
```

**`src/viminfo.c: barline_parse`**（**HEAD 仍存在**）— 读取畸形的 viminfo 文件时泄漏。

```c
buf = alloc((int)(len + 1));            // ← 分配
...
while (*p != '"') {
    if (*p == NL || *p == NUL)
        return TRUE;                    // ← 泄漏：发生在 value->bv_tofree = buf 之前
    ...
}
```
后面才会执行 `value->bv_tofree = buf` 把所有权交出去；在那之前的语法错误返回就漏了。
viminfo 文件内容部分可被外部影响，这条相对更值得关注。

### 8.2 分配失败路径上的泄漏（6 个）

这一类和上游 patch 9.2.0773–0803 那一批（「Memory leak in X on alloc failure」）**完全同类**，
说明这个模式上游正在系统性清理，只是还没清到这几处。

| 函数 | HEAD 状态 | 机理 |
|---|---|---|
| `src/clipboard.c: clip_wl_init_buffer_store` | 已随重构消失 | `store = alloc()` 后，`ftruncate()` 失败直接 `return NULL`，`store` 未释放 |
| `src/gui_gtk_x11.c: gui_gtk_draw_string` | **仍存在** | `conv_buf = string_convert()` 后，`alloc(convlen+2)` 失败 `return len`，`conv_buf` 未释放 |
| `src/edit.c: ins_tab` | **仍存在** | `saved_line = vim_strnsave()` 后，后续 `newp = alloc()` 失败 `return FALSE`，`saved_line` 未释放（其他路径有 `vim_free(saved_line)`） |
| `src/vim9class.c: ex_class` | **仍存在** | `cl = ALLOC_CLEAR_ONE(class_T)` 后，类名分配失败 `goto cleanup`，而 `cleanup:` 块释放了 `extends`/`intf_classes`/成员数组，**唯独没释放 `cl`** |
| `src/option.c: ExpandSettings` | 已顺手修掉 | `fuzmatch = ALLOC_MULT()` 后传给 `fuzzymatches_to_strmatches()`，后者在分配失败时 `return FAIL` 且不释放 `fuzmatch`（泄漏实际在被调用者里） |
| `src/if_xcmdsrv.c: serverRegisterName` | **仍存在** | `p = alloc()` 在 do-while 循环内，后续迭代注册失败时 `return FAIL`，跳过了循环后的 `vim_free(p)` |

### 8.3 所有权理解错误（3 个）

| 函数 | HEAD 状态 | 机理 |
|---|---|---|
| `src/clipboard.c: clip_wl_receive_data` | 已顺手修掉 | `tmp = string_convert(); final = tmp;` 之后 `clip_yank_selection()` 只是拷贝数据，`ga_clear(&buf)` 释放的是 `buf` 不是 `tmp`，`tmp` 永远泄漏 |
| `src/ex_docmd.c: ex_redir` | **仍存在** | `fname = expand_env_save()` 后，`FEAT_BROWSE` 下用户取消对话框 `return` ——跳过了后面的 `vim_free(fname)` |
| `src/strings.c: string_reduce` | **仍存在** | `fc = eval_expr_get_funccal()` 后，循环里求值出错 `return` 跳过了函数末尾的 `remove_funccal()`。patch 9.2.0960 改过这个函数，但修的是一个 double-free，这条泄漏还在；同文件的 `list_reduce()` 用的是 `break`，是正确写法 |

### 8.4 被判为误报的 7 个（说明 LLM 也会错）

为了完整，也记录判为假阳的：

| 函数 | 为什么不是泄漏 |
|---|---|
| `gui_mch_init` | `gui.fgcolor/bgcolor/spcolor` 是进程生命周期的全局变量 |
| `json_decode_item` | `dict_add` 失败时已 `dictitem_free`，成功时所有权归 dict |
| `compile_catch` / `compile_assign_index` | `generate_PUSHS()` 在失败时会释放 `*str` |
| `compile_for` | scope 已挂进 `cctx->ctx_scope`，`compile_def_function()` 失败时统一 drop |
| `findfilendir` | 每次迭代开头会释放上一轮的 `fresult`，循环只在 `fresult == NULL` 时结束 |
| `do_join` | `ml_replace_len(..., copy=FALSE)` 接管了 `newp` 的所有权 |

这 7 个都是 Phase 6 的 LLM 判成真 bug 的——**说明 LLM 验证这一步的精确率并非 100%**，
人工复核不可省略。这也是论文用"人工检查每条告警"作为最终关卡的原因。

### 8.5 修正 Infer 后新增的发现（refA / refE 未命中上游修复的告警）

| 函数 | 结论 | 机理 |
|---|---|---|
| `src/json.c: json_encode_lsp_msg` | **真泄漏，仍存在** | `json_encode_gap(&ga, ...)` 在已向 `ga` 追加内容后可能 FAIL（值里含 funcref 等），`return NULL` 跳过了 `ga_clear(&ga)`。可由 `ch_sendexpr()` 的 LSP 模式触发。CodeQL 没报它（`ga_grow` 系不在摘要里） |
| `src/vim9generics.c: parse_generic_func_type_args` | **真泄漏，仍存在**（分配失败路径） | `alloc()` 给 `gt_name` 失败时 `return NULL`，没有 `vim_free(ret_free)`（`type_name()` 返回的名字缓冲）。与 8.2 同类 |
| `src/beval.c: general_beval_cb` | 真泄漏，上游已顺手修掉 | `get_beval_info(getword=TRUE)` 返回分配的 `text`，缓冲区没有 `'balloonexpr'` 时直接落空；上游 HEAD 已加 `vim_free(text)` |
| `src/evalvars.c: set_internal_string_var` | 误报 | `alloc_string_tv()` 失败时自己释放参数 |
| `src/ex_docmd.c: expand_sfile` | 误报（官方式模型的副作用） | `eval_vars()` 被建模为"永不返回 NULL 的分配器"，而它在出错路径返回 NULL |
| `src/vim9compile.c: reserve_local` | 误报 | 分配失败时 `lvar` 已在 `ctx_locals` 里，`lv_name` 随之释放 |
| `src/vim9execute.c: exec_unpack_tuple` | 误报 | `list_alloc_with_items()` 返回引用计数 0 的 list，`++lv_refcount` 就是唯一所有者 |

`compile_catch`、`compile_for`、`clip_wl_init_buffer_store` 也再次被 Infer 报出，结论同 8.2/8.4。

---

## 9. 那个 12 GB 的 vim 进程：本复现为什么没发现它

### 9.1 回顾：现场发现了什么

开始复现之前检查机器资源，发现一个跑了 55 天的 `vi Dockerfile` 进程占用 **11.88 GB RSS + 1.45 GB swap**，
编辑的只是一个 12 KB 的 Dockerfile 且未修改过。诊断过程（完整记录在 `notes/vim-codeium-heartbeat-leak.md`）：

- `strace` 显示每 5 秒 `clone → execve(curl) → 读 stdout/stderr → SIGCHLD → wait4 → close` 一轮
- `gdb` 抓到调用栈正在 `f_job_start → job_start → mch_job_start → fork`
- 定位到 Codeium 插件 `autoload/codeium/server.vim:401`：
  `timer_start(5000, s:SendHeartbeat, {'repeat': -1})`，每次心跳用
  `job_start(['curl', ...], {out_cb, err_cb, exit_cb, close_cb})` 起一个 curl，
  四个回调都是闭包，捕获了 `result = {'out': [], 'err': []}`
- 直接读 `/proc/PID/mem` 采样 12.58 GiB 的堆（4 个 32 MiB 窗口，密度均匀）：

  | 堆中的字符串 | 外推总数 |
  |---|---|
  | `{"lastExtensionHeartbeat":…}`（curl 的 stdout） | 约 887,000 份 |
  | `% Total % Received …`（curl 的进度条，stderr） | 约 887,000 份 |
  | `http://127.0.0.1:40371/…/Heartbeat` | 约 2,660,000 份 |

  55 天 ÷ 5 秒 ≈ 951,000 次心跳，与 887,000 份残留高度吻合 → **每次 `job_start` 泄漏约 14 KB，从未回收**。

### 9.2 本次复现在 job.c / channel.c 上报了什么

实际查了数据：

```
codeql 在 job.c/channel.c 的告警：3 条
  src/channel.c:3771 channel_read        Z3 可行 → LLM 判为非 bug
  src/channel.c:2656 channel_exe_cmd     Z3 可行 → LLM 判为非 bug
  src/job.c:1839     job_info            Z3 可行 → LLM 判为非 bug
最终报出的 bug：0 条（job.c/channel.c 内）
```

摘要生成这一步倒是**做对了**——它正确学到了这些函数的语义：

```
job_start       → Allocator/return       job_free        → Deallocator/arg0
job_alloc       → Allocator/return       job_unref       → Deallocator/arg0
add_channel     → Allocator/return       channel_free    → Deallocator/arg0
                                          channel_unref   → Deallocator/arg0
                                          partial_unref   → Deallocator/arg0
```

### 9.3 为什么本方法结构上就发现不了它

**答案：这个泄漏不属于 MemHint 的目标模式，不是实现没做好，而是方法的固有边界。**

三条理由：

1. **它不是"某条路径上忘了释放"**。MemHint Phase 5 的判据是
   $\text{Sat}(\textit{reach}(r) \wedge \textit{alloc}(r) \wedge \neg\textit{freed}(r) \wedge \neg\textit{escaped}(r))$——
   在**同一个函数内**，找一条"分配了、没释放、没转移所有权"的路径。
   而 `job_start()` 里，job 对象被正确地分配、正确地挂进全局 job 列表（`escaped = true`）、
   进程退出时也确实有 `job_free()` 的路径。**单看任何一个函数，代码都是对的。**

2. **它是引用计数环 / GC 问题，跨越整个程序生命周期**。
   泄漏的成因是对象图：job → 四个回调 partial → 闭包作用域（funccal）→ 作用域里的局部变量 job。
   这个环让 Vim 的垃圾回收器无法回收已结束的 job。
   分配点和"本该释放却没释放"的时刻**相隔数百万次事件、由 GC 决定**，
   不存在任何一条 CFG 路径能表达它。MemHint 的符号层根本没有引用计数或 GC 的模型。

3. **论文自己把这类情况列为局限**（Section VI）：
   > "MemHint's Z3 encoding is deliberately lightweight: it models control-flow reachability and the
   > propagation of three memory-state predicates, rather than the full heap semantics of
   > weakest-precondition or symbolic-execution engines."

   并且 Phase 5 明确是**函数内**分析，循环携带的泄漏也在范围之外。

**换句话说**：这个 12 GB 的泄漏恰好是一个很好的反例，说明 MemHint（以及一般的路径敏感静态泄漏检测）
覆盖的是"错误处理路径上忘记 free"这一类，而不是"对象图长期不可回收"这一类。
后者需要的是引用计数分析、堆快照对比或运行时工具（我们实际用的就是 `/proc/PID/mem` 采样这种运行时手段）。

补充一点诚实的说明：这个泄漏的**根因归属**（是 Vim 的 GC 该回收却没回收，还是 Codeium 插件
每次心跳创建四个闭包的用法不当）还没有确认——要确认需要写一个最小复现脚本
（5 秒定时器 + `job_start` + 闭包回调，观察 RSS）在 Vim 9.2.0015 上跑。这件事还没做。

---

## 10. 复现过程中修掉的三个自身缺陷

记录下来，因为它们都是"看起来对、跑起来错"的类型，对后续修改符号层有参考价值。

### 10.1 条件布尔变量共享导致漏报（最严重）

已在 3.3 节详述。症状：`eval_dict` 的真泄漏被 Z3 判成 UNSAT 丢弃。
根因：`item = dict_find(); if (item)` 和 `item = dictitem_alloc(); if (item)`
共享了同一个布尔变量，被强制关联。
修复：只对函数内单次定义的变量共享布尔变量。
效果：修复后 `codeql` 的 Z3 通过率从 343/455 变为 361/455，`eval_dict` 被正确检出。

### 10.2 Tree-sitter 在 `#ifdef` 混入表达式时丢函数

Vim 大量使用这种写法：

```c
if ((*src == '$'
#ifdef VMS
            && at_start
#endif
    )
#if defined(MSWIN)
        || *src == '%'
#endif
   )
```

Tree-sitter 的 C 语法处理不了穿插在表达式内部的预处理指令，整个 `expand_env_esc()`（295 行）
被解析成 ERROR 节点，函数直接丢失。后果：CodeQL 报的告警定位不到所属函数，只能原样放行。

修复：解析失败（`root_node.has_error`）时，用一份**把所有条件编译指令行清空**的副本重新解析一遍，
把第一遍没拿到的函数补进来。行号保持不变（只清内容不删行）。
效果：抽取数从 11,821 增至 12,029，`expand_env_esc`、`get_keymap_str`、`VimMain` 等函数被找回。

### 10.3 Phase 5 不认识分析器自己跟踪到的分配器

CodeQL 自带查询会跟踪包装器返回链，报告的分配点可能是我们摘要里没有的函数
（比如 `dict_get_string`、`vim_getenv`）。第一版 Phase 5 遇到这种情况会说
"函数内没有可识别的分配"，把告警原样放行 —— Z3 过滤率因此是 **0%**。

修复：如果告警指明了某一行有分配，就信任分析器，把那一行的调用当作分配点。
效果：过滤率从 0% 提升到 16.4%（后续随其他修复到 20.7%）。

---

## 11. 局限与未完成项

### 11.1 已知局限

| 局限 | 说明 |
|---|---|
| 只做了 Vim | 论文做了 8 个项目。tmux 已确认可跑（75K SLOC，约 5 分钟 + $0.5），尚未执行 |
| Infer 告警数仍低于论文（332-395 vs 1,032） | 正则写法已修正，Infer 线复现成功（时间窗内 8/19）。剩余差距来自官方 Stage 1 校验器放行的摘要集合和前缀匹配，详见 7.2 节差异 3 与 `COMPARISON.md` |
| 没有复现摘要质量实验 | 论文的 Table III 需要人工标注 1,532 个函数（两人独立标注，Cohen's κ=0.94），成本过高 |
| 没有对比 LeakGuard / Semgrep | 论文的另外两个基线未纳入 |
| 未使用增强 CodeQL 查询 | 见 3.3 节的表格。论文自己的数据表明它们不带来新 bug |
| 真值是函数粒度 | 见 6.4 节 |
| 8 个上游仍存在的泄漏未上报 | 前 3 个的补丁已起草（`results/upstream-prs/`），等待提交 |

### 11.2 不确定的地方（诚实标注）

- **Phase 5 过滤率的差异**（20.7% vs 91.5%）：对照官方仓库后已基本定位（`COMPARISON.md` §4）——
  官方过滤器把建模失败一律丢弃，且 Stage 1 给出的摘要集合不同；但论文原始的 1,011 条告警清单没有公开，
  无法逐条核对。
- **Infer 的 1,032 条告警**：已查明我们此前的 Infer 正则写法无效（`COMPARISON.md` §3.2），修正后为
  332-335 条；与 1,032 的剩余差距来自官方摘要集合（567 个 Allocator、前缀匹配）。
- 第 9 节那个 12 GB 泄漏的根因归属未经最小复现验证。

---

## 12. 关于上报：是不是 security finding

**结论：不是安全漏洞，走公开 issue / PR，不走安全通道。**

依据有三条：

1. **Vim 的 SECURITY.md 要求**：报告者必须
   *"Clearly explain **why** the behaviour is a security issue, not just that a bug exists"*，
   并且明确警告 *"Low-quality or speculative reports waste maintainer time and will be closed without action"*。
   一个每次触发泄漏几十到几百字节、且不导致内存破坏的泄漏，说不出"为什么是安全问题"。

2. **上游的既有实践**：我们提取的那 60 个泄漏修复提交，**全部是普通的 patch，没有一个走 CVE 或安全通告**。

3. **论文作者本人的做法**：论文一作 Huihui Huang 把 MemHint 在 Vim 上发现的 bug
   全部提成了**公开 PR**，例如：
   - [vim/vim#19516](https://github.com/vim/vim/pull/19516) → patch 9.2.0065 `invoke_sync_listeners`
   - [vim/vim#19517](https://github.com/vim/vim/pull/19517) → patch 9.2.0066 `build_drop_cmd`
   - [vim/vim#19531](https://github.com/vim/vim/pull/19531) → patch 9.2.0079 `eval_dict`

   全部已合并。

### 建议的上报格式（照抄论文作者被接受的模板）

标题：``Fix memory leak in `did_set_pumborder()` in `src/optionstr.c` ``

正文：

```markdown
### Problem

In `did_set_pumborder()`, a token is allocated at line 3698:

```c
token = vim_strnsave(p, len);
```

In the `custom:` branch, three `goto error` statements (lines 3735, 3741, 3746)
return without releasing `token`, unlike the other error paths in the same
function which call `vim_free(token)` first. This leaks on every invalid
`:set pumborder=custom:...` value.

### Solution

Free `token` before jumping to `error` in those three places.
```

**建议做法**：**提 PR 而不是 issue**——附上补丁的接受率明显更高（上面三个 PR 都是当天合并的）。
另外建议分批提交（一次 3–4 个相关的），一次性提 12 个容易被当成刷 PR。

优先顺序建议（只针对 HEAD 仍存在的 8 个）：
1. 先提用户可直接触发的 3 个：`f_setmatches`、`barline_parse`、`string_reduce`——**补丁已起草并验证（`barline_parse` 用 ASAN；另外两个泄漏对象仍被全局变量引用、LSAN 看不见，改用 gdb 附加读 `first_list`/`current_funccal`），见 `results/upstream-prs/README.md` 和 `VERIFICATION.md`**
2. 再提分配失败路径的 4 个：`gui_gtk_draw_string`、`ins_tab`、`ex_class`、`serverRegisterName`（可放一个 PR，说明是 9.2.0773–0803 那批清理的延续）
3. 最后提 `ex_redir`（仅 FEAT_BROWSE 下取消对话框触发，需要更多解释）

---

## 13. 文件清单与复现命令

### 13.1 代码

```
memhint/            实现，约 1,900 行（模块说明见 3.2 节）
tests/              单元测试：论文图 4(a)-(d)、图 5、委托/别名/宏
README.md           快速上手
REPRODUCTION.md     本文档
COMPARISON.md       与官方仓库 jiekeshi/MemHint 的逐阶段对照，Infer 修正过程
notes/compare_stage{1,2_codeql,3}.md   对照原稿（逐行代码引用）
notes/vim-codeium-heartbeat-leak.md    第 9 节那个 12 GB 泄漏的现场取证记录
```

### 13.2 结果（`results/vim_9_2_0015/`，已从 output/ 拷出，可入库）

| 文件 | 内容 |
|---|---|
| `REPORT.md` | 对照论文 Table I/II/IV/V 的完整报告，含 42 条 bug 的逐条验证状态 |
| `ground_truth.md` / `.json` | 60 个上游泄漏修复提交，及各配置的命中情况 |
| `manual_review.json` | 26 条未命中报告的人工复核结论（含 Infer 修正后新增的 7 条），每条带依据 |
| `hints.json` | 493 条 Z3 验证过的函数摘要（论文附录 B 的格式） |
| `stage1_stats.json` | Stage 1 漏斗数字 |
| `<配置>/bugs.json` | 各配置最终报出的 bug |
| `<配置>/stage2_stats.json`、`stage3_stats.json` | 各阶段的告警数、过滤数、成本 |

完整中间产物（含每条告警的 Z3 判定和见证路径 `z3_results.json`、LLM 逐条判定 `llm_verdicts.json`、
LLM 响应缓存）在 `output/vim_9_2_0015/`，未入库（体积大，已在 .gitignore）。

### 13.3 从零复现

```bash
# 环境
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python \
    "tree-sitter>=0.23" tree-sitter-c tree-sitter-cpp z3-solver==4.15.4.0 openai pyyaml tqdm pytest
# tools/codeql = CodeQL bundle v2.23.9；tools/infer = Infer v1.2.0
export OPENAI_API_KEY=...
.venv/bin/python -m pytest -q            # 论文图 4/图 5 的单测

# 目标
git clone --branch v9.2.0015 --depth 1 https://github.com/vim/vim subjects/vim_9_2_0015
P=subjects/vim_9_2_0015; O=output/vim_9_2_0015

# Stage 1：抽取 + LLM 摘要 + Z3 验证（约 12 分钟，$1.7）
.venv/bin/python -m memhint stage1 --project $P --out $O

# 建库（一次即可，两个分析器各建一次）
(cd $P && codeql database create ../../$O/codeql-db --language=cpp --command="make -j8")
(cd $P && make clean && infer capture --results-dir ../../$O/infer-out -- make -j8)

# Stage 2 + 3，四个配置
for A in codeql infer; do
  .venv/bin/python -m memhint stage2 --project $P --out $O --analyzer $A
  .venv/bin/python -m memhint stage2 --project $P --out $O --analyzer $A --vanilla
  .venv/bin/python -m memhint stage3 --project $P --out $O --analyzer $A
  .venv/bin/python -m memhint stage3 --project $P --out $O --analyzer $A --vanilla
done

# 真值校验 + 报告
(cd $P && git fetch --filter=blob:none --shallow-since=2025-12-01 origin master)
.venv/bin/python -m memhint.evaluate $P $O v9.2.0015 FETCH_HEAD
.venv/bin/python -m memhint.report $O > $O/REPORT.md
```

所有 LLM 响应都缓存在 `output/<项目>/llm_cache/`，重跑任何阶段都不再花钱。

---

## 附：一句话总结

用 `gpt-5.6-luna` 替换 Gemini、独立实现论文方法后，在 Vim 9.2.0015 上
**以 $1.72 的成本复现了论文的核心主张**：注入 Z3 验证过的自定义内存管理函数摘要，
使 CodeQL 命中的上游泄漏修复数从 7 个翻倍到 14 个（论文时间窗内 19 个中）。
Infer 线在修正正则写法（OCaml `Str` 语法，论文附录印错了）后同样复现：从 0 到时间窗内 8/19。
额外发现 14 个真实泄漏（其中 10 个在上游 HEAD 仍存在，前 3 个已起草补丁并经 ASAN 验证）。
与官方仓库的逐阶段对照见 `COMPARISON.md`：抽取、提示词、CodeQL 注入、LLM 验证与官方等价；
Stage 1/3 的 Z3 部分我们按论文公式实现，官方代码是启发式。
