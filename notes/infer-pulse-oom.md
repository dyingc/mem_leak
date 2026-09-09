# Pulse 单 worker 内存增长与 `Fatal error: out of memory`：证据、根因与修复

> 2026-09-09。对应请求文档 `/tmp/b2-obligation-recall/e1-infer-parser-oom-fix-request.md` 的 Problem 2。
> 补丁：`notes/infer-pulse-oom.patch`（Infer v1.2.0-4c53e80 源码，`tools/infer-src`）。
> 工作目录（gitignored）：`output/oom/`——脚本在 `tools/`，语料在 `corpus/`，每次运行在 `runs/<名字>/`
> （`summary.txt`、`command.txt`、`precheck.txt`、`time.txt`、`host_samples.txt`、`stdout/stderr.txt`、`infer-out/`）。

## 0. 结论先行

- **请求文档里的精确故障（约 45 s 内 RSS 7.0 GiB 后 `Fatal error: out of memory`）没有在本机用通用夹具复现。**
  本机最大的通用夹具（Vim 9.2.0015 最大的几个 TU，单 worker、`ulimit -v 8388608`）峰值托管堆 3.1–4.0 GiB、RSS 最高 4.7 GB，都在 8 GiB 内完成。
  机制本身已测量清楚（§3）；“把上限降到与本机峰值同量级（4 GiB / 2 GiB）时对照二进制以同样的
  `Fatal error: out of memory` 退出、修复版在同一上限下完成”这组验证运行已排队，**结果尚未出来**（§5 会更新）。
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

补丁基于已应用 `notes/infer-arg-models.patch` 与 `notes/infer-argfile-transport.patch` 的 v1.2.0-4c53e80 源码树
（`scripts/build-patched-infer.sh` 第 6 步按顺序应用三个补丁；单独应用见 §7）。改动 10 个文件、579 行 diff：

| 文件 | 改动 | 默认行为是否改变 |
|---|---|---|
| `backend/Summary.ml`/`.mli` | 摘要缓存条目带 `last_use` 时钟；新增 `evict_if_heap_too_big`：`add` 时若主堆 ≥ `--summary-cache-max-heap-GB` 且距上次驱逐 ≥ `--compaction-minimum-interval-s`，删除最近最少使用的一半条目并 `Gc.compact`；`cache_stats` 供诊断 | 否（选项默认 0 = 关闭） |
| `base/Config.ml`/`.mli` | 新选项 `--summary-cache-max-heap-GB`（默认 0）、`--gc-space-overhead`（默认 120）；`--compaction-if-heap-greater-equal-to-GB` 默认 8→1 并说明原因 | 压缩阈值：实际生效值不变（1 GiB） |
| `backend/InferAnalyze.ml` | 阈值换算改为 `GB·2^30 / (word_size_in_bits/8)`；日志打印真实 GB（两位小数）；目标结束后的 HeapTrace 钩子 | 日志数值改为真实值 |
| `pulse/Pulse.ml` | `AboutToOOM` 处理增加 `record_oom_abort`：写 `pulse/oom-aborted-procedures-<pid>.txt`（proc_uid、文件、行号、压缩后堆字数）、progress 日志、`Stats.incr_pulse_oom_aborts`；`exec_instr` 的指令级突增追踪（仅 `INFER_HEAP_TRACE` 时） | 只多了显式记录 |
| `base/Stats.ml`/`.mli` | 计数器 `summary_cache_evictions`、`pulse_oom_aborts`（进入 `stats/` 与日志里的 stats 输出） | 否 |
| `backend/ondemand.ml` | 每个过程分析开始/结束、每个顶层过程结束的 HeapTrace 钩子；`INFER_CLEAR_SUMMARY_CACHE_PER_PROC` 诊断开关 | 否 |
| `absint/HeapTrace.ml`（新） | 诊断模块，环境变量控制，默认完全惰性 | 否 |

**为什么驱逐缓存不丢结果**：`Ondemand.run_proc_analysis` 的 `postprocess` 先 `Summary.OnDisk.store`（写入 `results.db` 的 `specs`，DBWriter 会与旧 Pulse payload 合并）再把同一记录放进缓存；
`Summary.OnDisk.get` 未命中时走 `load_summary_to_spec_table` 从库里反序列化并回填。正在分析中的过程不经过缓存被读取（`analyze_callee` 先用 `is_active` 做递归环检测）。
`add_errlog` 找不到内存副本时只更新库，库里的 report_summary 是权威。所以驱逐只影响 CPU（重新反序列化），不影响报告内容。

**为什么不直接调大 `--pulse-max-heap`、`--pulse-max-disjuncts` 之类**：它们要么跳过过程、要么少走路径（文档明确不接受）；本补丁只回收“已经落盘的缓存”与 GC 的空闲空间。
`--pulse-max-heap` 保留原语义，但触发时现在是显式、可核对的（§5 的完整性脚本会把这些过程列为 “pulse-max-heap abort (explicit)” 并判 INCOMPLETE）。

## 5. 验证：复现、修复前后对比、完整性

完整性检查：`python3 output/oom/tools/check_completeness.py <results-dir> --infer <infer> [--files <changed-files-index>]`——
用 `infer debug --procedures --procedures-name --procedures-source-file --procedures-definedness` 列出全部已捕获且有定义的过程（可限制到本次调度的文件），
对照 `results.db` 里 `specs.Pulse IS NOT NULL` 的过程；缺失的按 `pulse/oom-aborted-procedures-*.txt`、`logs` 里的
`Skipped large procedure`、`TIMEOUT in pulse after` 归因，其余标 UNEXPLAINED；同时输出 `PRAGMA integrity_check`。零缺失才是 COMPLETE。

已完成的基线见 §3.3（全部 COMPLETE）。以下 A/B 运行已排入 `output/oom/runs/q2.txt`（`results/infer-oom/runs/queue-2.txt`），
**结果尚未出来，本节会在运行结束后更新**：

| 运行 | 二进制 | `ulimit -v` | 额外参数 | 预期 |
|---|---|---|---|---|
| `vim-ex_docmd-cap4-control` | 对照 | 4 GiB | — | 复现 `Fatal error: out of memory`（基线峰值堆 4.05 GiB） |
| `vim-ex_docmd-cap4-fix1` | 修复 | 4 GiB（wall 1800 s） | `--summary-cache-max-heap-GB 1` | 完成，COMPLETE |
| `c5-cap2-control` | 对照 | 2 GiB | — | 复现 OOM（基线峰值堆 2.01 GiB） |
| `c5-cap2-fix1` | 修复 | 2 GiB（wall 1800 s） | `--summary-cache-max-heap-GB 1` | 完成，COMPLETE |
| `vim-ex_docmd-fix1` | 修复 | 8 GiB（wall 1800 s） | `--summary-cache-max-heap-GB 1` | 峰值明显低于 4.12 GB，报告与 `vim-ex_docmd` 一致 |
| `vim-ex_docmd-gc80` | 修复 | 8 GiB（wall 1800 s） | `--gc-space-overhead 80` | 量化 GC 放大系数的影响 |

两 worker 实验（文档第 6 条）要等单 worker 修复运行稳定完成后再做。

## 6. 未解决的问题与阻塞

- 精确故障未复现：本机没有一个通用夹具在 8 GiB 上限下越界；只能在降低上限后复现同一机制。要在请求方的环境验证，请按 §7 应用补丁重建，
  用 `results/infer-oom/tools/run_guarded.sh`（改 `INFER`、路径）跑“对照 / `--summary-cache-max-heap-GB 1`（或 2）”两组，并用 `check_completeness.py` 判定。
- `--pulse-max-heap 200000000` 挡不住 OOM 的原因未定位。本机指令级突增最多 355 MB；候选解释（未证实）：
  ① 请求方的 TU 里有远大于 Vim `u_compute_hash`（11.7 MB 序列化）的摘要，调用指令一次应用 20 个 pre/post 就跨过几 GB；
  ② 摘要序列化/入库瞬态（`Marshal` + SQLite blob 拷贝，`--sqlite-max-blob-size` 默认 500 MB）发生在两次检查之间。
  `INFER_HEAP_TRACE=<prefix> INFER_HEAP_TRACE_INSTR_MB=256` 在请求方环境跑一次就能回答（看 `instr-burst` 行）。
- 深度 260–430 层的嵌套按需分析（每层持有未完成的 invariant map）在峰值时刻已经退栈，所以本机数据里不是主因；在更大的 TU 上它可能与缓存叠加，未测。
- 超时（`--timeout 60`，CPU 时间）导致的 NULL 摘要在 Infer 里同样是静默的，只在 `logs` 有 debug 行；本补丁没有改这一点，完整性脚本会把它们列出。
- 单位修正把 `--compaction-if-heap-greater-equal-to-GB` 的默认值改成 1 以保持原实际行为；如果上游希望保留“8”这个数字，需要同时接受压缩频率下降 8 倍。

## 7. 复现命令

```bash
# 构建（三个补丁按顺序应用；--verify 只跑 Problem 1 的验收测试）
scripts/build-patched-infer.sh
# 或手工：cd tools/infer-src && git apply ../../notes/infer-arg-models.patch \
#   && git apply ../../notes/infer-argfile-transport.patch && git apply ../../notes/infer-pulse-oom.patch \
#   && source ../infer-env.sh && make -j8 opt

# 合成语料（确定性，seed=1）
python3 results/infer-oom/tools/gen_corpus.py --out output/oom/corpus/c5 --units 12 --procs 60 --branches 5 --calls 3 --loops 1 --fields 12
(cd output/oom/corpus/c5 && infer capture --results-dir infer-cap -- make && make clean)

# 受限单 worker 运行（8 GiB 虚拟内存、900 s、nice/ionice、--jobs 1 --max-jobs 1 --timeout 60）
INFER=<二进制> results/infer-oom/tools/run_guarded.sh c5-base output/oom/corpus/c5/infer-cap
INFER=<二进制> results/infer-oom/tools/run_guarded.sh c5-fix output/oom/corpus/c5/infer-cap --summary-cache-max-heap-GB 1
# 降低上限复现机制：VMEM_KB=2097152 …；堆追踪：INFER_HEAP_TRACE=<前缀> …
python3 results/infer-oom/tools/check_completeness.py output/oom/runs/c5-fix/infer-out --infer <二进制>
python3 results/infer-oom/tools/analyze_trace.py <前缀>.<pid>
```
（`run_guarded.sh` 里的绝对路径指向本仓库的 `output/oom/`；换环境时改 `BASE`/`INFER` 两行即可。）
