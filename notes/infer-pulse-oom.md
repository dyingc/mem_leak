# Pulse 单 worker 内存增长与 `Fatal error: out of memory`：证据、根因与修复

> 2026-09-09。对应请求文档 `/tmp/b2-obligation-recall/e1-infer-parser-oom-fix-request.md` 的 Problem 2。
> 补丁：`notes/infer-pulse-oom.patch`（Infer v1.2.0-4c53e80 源码，`tools/infer-src`）。
> 工作目录（gitignored）：`output/oom/`——脚本在 `tools/`，语料在 `corpus/`，每次运行在 `runs/<名字>/`
> （`summary.txt`、`command.txt`、`precheck.txt`、`time.txt`、`host_samples.txt`、`stdout/stderr.txt`、`infer-out/`）。

## 0. 结论先行

- **请求文档里的精确故障（约 45 s 内 RSS 7.0 GiB 后 `Fatal error: out of memory`）没有在本机用通用夹具复现。**
  本机最大的通用夹具（Vim 9.2.0015 最大的几个 TU，单 worker、`ulimit -v 8388608`）峰值托管堆 3.1–4.0 GiB、RSS 最高 4.7 GB，都在 8 GiB 内完成。
  故障机制则复现并测量到了：把上限降到与本机峰值同量级（4 GiB / 2 GiB）时，对照二进制以同样的
  `Fatal error: out of memory` 退出，修复后的二进制在同一上限下完成并通过完整性检查（§5）。
- 根因（已证实的部分，§3）：单 worker 模式下一个源文件是一个调度单元；文件内第一个顶层过程会按需分析整个传递闭包
  （Vim 一个大 TU ≈ 3.8k 个过程，嵌套深度 260–430），所有算出来的摘要都留在进程内的摘要缓存里直到整个文件结束才清；
  OCaml 主堆又按 `space_overhead=120` 放大到存活数据的约 2.2 倍；压缩阈值因单位错误实际是 1 GiB 而不是 8 GB，
  日志里的堆大小也虚高 8 倍（文档里的“26 GiB”真实约 3.3 GiB）。文件结束、清缓存并压缩后堆回落到 0.09 GiB：**不是泄漏，是保留 + GC 放大**。
- 修复（§4）：对 Infer 本身做四处修改，全部不丢结果——
  1. `--summary-cache-max-heap-GB N`：主堆达到 N GB 时驱逐最近最少使用的一半摘要缓存并压缩；摘要在入缓存前已写入 `results.db`，被驱逐的摘要下次使用时从库里重新读回。
  2. 修正压缩阈值的单位错误，默认值改为 1 GB（保持原来实际生效的行为），日志改为按字节的 GB。
  3. `--gc-space-overhead`（默认 120，保持原行为）可调 GC 放大系数。
  4. `--pulse-max-heap` 触发的“跳过当前过程”不再只在 `logs` 里留一行 internal error：写入
     `<results-dir>/pulse/oom-aborted-procedures-<pid>.txt`、打 progress 日志、计入 `stats` 的 `pulse_oom_aborts`；
     配合 `output/oom/tools/check_completeness.py` 把缺摘要的过程逐个列出并归因（OOM 中止 / CFG 过大 / 超时 / 未解释），只有零缺失才判定 COMPLETE。
- 单条指令、单个过程级别的突增在 Vim 上分别最多约 355 MB 与 0.78 GiB（§3.4），没有出现一条指令产生数 GB 的情况；
  请求文档中 `--pulse-max-heap 200000000`（1.6 GB）挡不住 OOM 的现象，本机没有观察到，列为未解决问题（§6）。

## 1. 安全执行方式（所有运行相同）

`output/oom/tools/run_guarded.sh <run> <capture-dir> [infer analyze 额外参数]`：

- 启动前检查 `MemAvailable > 12 GiB`、1 分钟负载 ≤ 2、暂存盘 ≥ 50 GiB（根分区 573 GB 可用；`/tmp` 是 15 GB 的 tmpfs，所以暂存目录放在 `/home` 下的 `output/oom/`）、没有别的 `infer` 进程；
- `setsid` 进程组 + `ulimit -v 8388608`（复现用例另注明）+ `nice -n 10` + `ionice -c 3` + 900 s 进程组 wall guard（修复后的长运行注明用 1800 s）；
- `infer analyze --results-dir … --pulse-only --jobs 1 --max-jobs 1 --timeout 60 …`，capture 目录先复制一份，`infer analyze` 自己会清空旧 specs；
- `tools/memwrap.py` 记录退出码、wall、用户/系统 CPU、`ru_maxrss`（峰值 RSS）和每秒进程树 RSS 采样；同时每 2 s 采样主机 `MemAvailable`/负载/`SwapFree`；
- 结束后记录 `report.json` 是否存在及条数、`logs` 里 OOM/压缩行数、GC `top_heap_words`（托管堆峰值）、`du` 结果目录。
- 队列：`tools/run_queue.sh runs/qN.txt` 顺序执行、每次只跑一个 Infer，构建期间自动等待。
- `ulimit -u 128` 从未使用；没有做任何系统级清理。

Infer 构建：`source tools/infer-env.sh && cd tools/infer-src && make -j8 opt`（增量约 2.5–4 min），二进制 `tools/infer-src/infer/bin/infer`，
`infer --version` = `Infer version v1.2.0-4c53e80`。对照二进制是加诊断/修复之前的同源构建快照 `output/oom/tools/infer-pre/bin/infer`
（含 Problem 1 的 argfile 修复与参数位置模型补丁，两者都与内存无关）。

## 2. 夹具

| 夹具 | 内容 | capture | 备注 |
|---|---|---|---|
| `corpus/c1`…`c5` | `tools/gen_corpus.py` 生成的多 TU C 语料：结构体/数组/别名、嵌套分支（2^k 路径→撞 20 个 disjunct 上限）、循环里的函数指针调用、malloc/free、跨 TU 随机调用、跨 TU 递归环 `r0→r1→…→r0` | make，`infer capture -- make` | c5 = 12 TU × 60 过程，27 087 行，739 个过程，capture 4.6 MB |
| `corpus/s1`/`s2` | `tools/gen_sha.py`：两份改名的 SHA-256（s2 为 64 轮完全展开的直线代码）+ 逐行哈希的调用者 + 200 个 case 的解释器 switch | make | 用来测“算术密集→符号状态爆炸”假设 |
| `corpus/vim` | Vim 9.2.0015 的 capture（`output/vim_9_2_0015/infer-out/capture.db`，152 个 TU、10 714 个过程，其中 9 920 个有定义），用 `--changed-files-index` 选 TU | 已有 | 本机最大的通用夹具 |

## 3. 证据与根因

### 3.1 压缩阈值的单位错误（已证实，源码 + 日志）

`infer/src/backend/InferAnalyze.ml`：

```ocaml
let compaction_if_heap_greater_equal_to_words =
  Config.compaction_if_heap_greater_equal_to_GB * 1024 * 1024 * (1024 / Sys.word_size_in_bits)
...
L.log_task "Triggering compaction, heap size= %d GB@\n" (heap_words * Sys.word_size_in_bits / 1024 / 1024 / 1024)
```

`Sys.word_size_in_bits = 64`，所以默认 8 “GB” 换算成 `8·2^20·16 = 2^27` 个字 = **1 GiB**，日志打印的是 Gbit（真实值 × 8）。
本机 `runs/vim-evalfunc` 的 `logs` 打印 `Triggering compaction, heap size= 28 GB`，同一运行的 GC 统计 `top_heap_words = 472 331 264` = 3.52 GiB。
请求文档中“压缩时托管堆约 26 GiB”按同一换算约 3.3 GiB。这个错误不会导致崩溃（崩溃来自 OCaml 运行时在 minor GC 提升对象时扩堆失败——
`runtime/memory.c:516 caml_fatal_error("out of memory")`，Infer 1.2 没有任何“看内存太多就退出”的逻辑；`--oom-threshold` 只在多进程池里节流且默认关闭），
但它让阈值比设计低 8 倍、也让所有基于日志的内存估计高 8 倍。

### 3.2 单 worker 模式下内存的去向（已测量）

诊断补丁 `infer/src/absint/HeapTrace.ml`（默认关闭，`INFER_HEAP_TRACE=<前缀>` 时每次过程分析开始/结束、每个顶层过程结束、每个调度目标结束记录
`Gc.quick_stat` 的 `heap_words`/`top_heap_words`/GC 次数；`Obj.reachable_words` 量摘要缓存；单条 Pulse 指令使主堆增长 ≥ 64 MB 时记录该指令，
并扣除指令内部嵌套按需分析的增长）。分析脚本 `tools/analyze_trace.py`。

**（a）峰值出现在第一个顶层过程的按需闭包里，不是跨文件累积。** `runs/vim-top8-trace`（8 个最大 TU 一起，999 s）：
第一个顶层过程 `eval.c:eval4` 耗时 592 s，期间堆从 0 涨到 3.06 GiB（整个运行的峰值）；这一个过程按需触发了 142 个文件、5 780 个过程的分析，
嵌套深度最深 268 层（`c5` 上 433 层）。之后 7 个文件各自 `target-done` 时堆 0.09–0.57 GiB，峰值不再上升。
因此故障是“**单个文件目标内累积**”（对应文档里的“不能定位到唯一失败过程”），不是某一个过程独占，也不是跨文件累积。

**（b）保留的东西主要是摘要缓存 + GC 放大，文件结束就能全部回收。** `Summary.OnDisk.cache` 只在 `InferAnalyze.analyze_target` 每个目标结束时清（`clear_caches`），
单 worker（`--jobs 1`）的目标是整个文件（`InferAnalyze.analyze`：`Tasks.run_sequentially` over `File` targets）。实测：

| 运行 | 文件结束前缓存条目 / 可达大小 | 托管堆峰值 | RSS 峰值 | 目标结束+压缩后堆 |
|---|---|---|---|---|
| `vim-ex_getln-trace` | 3 880 条 / 1.29 GiB | 3.06 GiB | 4.68 GB | 0.09 GiB |
| `vim-vim9execute-trace` | 3 841 条 / 1.30 GiB | 3.06 GiB | 4.66 GB | 0.09 GiB |
| `c5-trace` | 708 条 / 0.40 GiB | 2.01 GiB | 3.03 GB | 0.27 GiB |

Infer 在 `Config.ml` 里把 `space_overhead` 设为 120（主堆允许到存活数据的 ~2.2 倍），所以 1.3–1.4 GiB 存活对应 3.06 GiB 主堆。
RSS 再高出 1.6 GB 来自 `caml_compact_heap` 的二次压缩：它先按“存活 + 开销”分配一整块新 chunk 再搬迁（`runtime/compact.c`），瞬态 = 旧堆 + 新块；
在 `ulimit -v` 下这块分配失败只是放弃二次压缩，不致命。

**（c）在 `--jobs 4` 的历史运行里同样的机制以每个 worker 为单位出现**：`output/vim_9_2_0015/infer-out*/logs` 里各 worker 打印
`Triggering compaction, heap size= 8…19 GB`（真实 1.0–2.4 GiB），与文档中多 worker 时“托管堆 26 GiB”一致。

### 3.3 本机最大通用夹具在 8 GiB 上限下的基线（对照二进制）

| 运行 | wall | 峰值 RSS | 托管堆峰值 | 报告 | 完整性 |
|---|---|---|---|---|---|
| `vim-evalfunc`（12 813 行） | 664 s | 3.56 GB | 3.52 GiB | 35 条 | 完整 |
| `vim-ex_docmd`（10 481 行） | 750 s | 4.12 GB | 4.05 GiB | 48 条 | 完整 |
| `vim-os_unix`（10 699 行） | 524 s | 3.33 GB | 3.06 GiB | 33 条 | 完整 |
| `vim-undo`（含 11.7 MB 的最大摘要 `u_compute_hash`） | 632 s | 3.58 GB | 3.06 GiB | 31 条 | 完整 |
| `vim-top8-trace`（8 个最大 TU） | 999 s（wall guard 3600 s） | 3.48 GB | 3.06 GiB | 71 条 | 完整 |
| `c3-base`（8 TU × 40 过程） | 579 s | 2.30 GB | 2.01 GiB | 2 条 | 完整 |
| `c5-trace`（12 TU × 60 过程） | 455 s | 3.03 GB | 2.01 GiB | 13 条 | 完整（736/736） |

所有运行 `PRAGMA integrity_check = ok`、退出码 0、`report.json` 存在；主机 `MemAvailable` 始终 ≥ 15 GiB、负载 ≤ 1.5、`SwapFree` 不变。
`--timeout 60` 触发次数：Vim 单 TU 运行 0 次，`top8` 与 `ex_getln` 各 1 次（日志形式是 `TIMEOUT in pulse after …`，超时的过程摘要为 NULL 且默认无任何用户可见提示，完整性脚本会把它们列出）。

### 3.4 单过程 / 单指令的突增（已测量，排除“一条指令几 GB”）

- 单个过程自身（扣除被调用者）使堆增长最多：`vim9execute.c:list_instructions` +0.78 GiB（33 s）、`option.c:did_set_undofile` +0.43、`undo.c:u_compute_hash` +0.42、`sha256.c:sha2_seed` +0.35。
- 单条指令自身最多 355 MB（`WaitForChar` 里调用 `ui_wait_for_chars_or_timer`），其余 ≤ 270 MB，多为把大被调摘要应用到 20 个 disjunct 的调用指令和 `EXIT_SCOPE`。
- 合成 SHA-256（循环版 s1 与 64 轮展开版 s2）在 Pulse 下都只用 6 s、0.28 GiB：Vim 的 `sha256.c` 之所以贵，不是算术本身，而是它的调用者闭包；这条假设没有站住。
- 因此在本机数据里，“两次 `--pulse-max-heap` 检查之间堆跳 6 GB”没有出现；`--pulse-max-heap 200000000` 挡不住 OOM 这一现象未复现（§6）。

### 3.5 证据分类

已证实：3.1 的单位错误；3.2 的“文件内按需闭包 + 摘要缓存 + 2.2× GC 放大 + 压缩瞬态”；3.3/3.4 的量级；超时与 OOM 中止都会静默留下 NULL 摘要。

假设（有部分证据）：请求文档的失败 TU 与 Vim 相同机制，只是闭包更大/摘要更大（文档中 `--pulse-max-heap 20000000` 完成但跳过几百个过程，
与“堆一旦被缓存撑到阈值以上就一直高于阈值”的行为一致）。

未解决：为什么 1.6 GB 的 `--pulse-max-heap` 挡不住 8 GiB；45 s 内 7 GiB 的速率（本机 Vim 约 60 s 1 GiB）。

## 4. 修复内容（`notes/infer-pulse-oom.patch`）

TBD_FIX_SECTION

## 5. 验证：复现、修复前后对比、完整性

TBD_RESULTS

## 6. 未解决的问题与阻塞

TBD_OPEN
