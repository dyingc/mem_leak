# 如果 OOM 仍未消失：怎么用新诊断在一轮内定位

给在目标压力用例上复跑的人。目标是**一次运行就拿到足以定位相位的证据**，不要再靠猜。
需要的二进制：应用了 `notes/infer-arg-models.patch`、`notes/infer-argfile-transport.patch`、
`notes/infer-pulse-oom.patch`、`notes/infer-pulse-oom-followup.patch` 后 `make -j8 opt` 的 Infer。

## 0. 先看三行日志，不用开追踪

新版在 `<results-dir>/logs` 里必然有这三类行，先看它们再决定要不要开追踪：

```
Memory: address space limited to 8.00 GB (RLIMIT_AS); relieving memory pressure above 4.00 GB,
        giving up on the current procedure above 6.00 GB
Memory: 50% / 75% / 90% of the ceiling in use (… words of …; heap … words)
Memory: peak managed heap X GB, peak address space Y GB, N relief(s), M procedure(s) given up on
```

- **第一行**说明这次运行到底有没有上限。如果它写的是 `Memory: unbounded.`，那就是没有加
  `ulimit -v`、也没有给 `--pulse-max-heap`，机制完全惰性，进程仍会被运行时杀掉——先加上限再谈其它。
- **第二行**给出内存爬升的轨迹。只有 50% 没有 75%，说明死得很突然（单次巨量分配）；
  三档都有，说明是稳步增长。
- **第三行**是收尾摘要。`M > 0` 表示有过程被显式放弃，名单在
  `<results-dir>/pulse/oom-aborted-procedures-<pid>.txt`，每行是：

  ```
  <过程唯一名>  <文件>  <行号>  heap_words=…  heap_words_grown_by_this_procedure=…
  ```

  最后一列是**该过程自身造成的增长**。它很大 ⇒ 这个过程就是罪魁；它接近 0 ⇒ 这个过程只是在
  别人已经把内存撑满时被启动，真正的罪魁在调用栈更外层（继续往下看 §2）。

如果进程仍然以 `Fatal error: out of memory` 死亡（第三行不存在），进入 §1。

## 1. 开结构化追踪跑一次

```bash
INFER_HEAP_TRACE=/path/to/scratch/heaptrace \
  <guarded runner> …   # 其余参数完全不变
```

产出 `<prefix>.<pid>.jsonl`，每行一个 JSON 记录，字段：

```
timestamp, pid, phase, procedure, source_file,
heap_words, top_heap_words, minor_collections, major_collections, compactions,
rss_kib, vm_size_kib, private_dirty_kib,
summary_cache_entries, summary_cache_reachable_words, serialized_payload_bytes
```

检查点覆盖：调度目标（`target-start` / `target-done` / `target-done-compacted`）、
顶层过程（`toplevel-done`）、每个按需过程（`proc-start` / `proc-end` / `proc-end-error` /
`proc-exception`——**异常路径也有**）、摘要序列化与写库（`summary-store-start` / `-end`、
`payload-serialized` 带字节数）、每次 `Gc.compact` 前后（`gc-compact-before` / `-after`）、
内存缓解（`memory-pressure-relief`）、给出放弃（`pulse-oom-abort`）、
指令级突增（`instr-burst`，默认阈值 64 MB）与指令异常（`instr-error`）。

用仓库里的脚本解读：

```bash
python3 results/infer-oom/tools/analyze_jsonl.py <prefix>.<pid>.jsonl
```

它直接打印：峰值 heap/RSS/VM、各相位计数、**死亡前最后 12 个检查点**、
**已开始且从未结束的过程（最内层在最后）**、以及按四条规则给出的解读。

## 2. 怎么读结论

| 观察 | 结论 |
|---|---|
| 只有 1 个过程“已开始未结束”，它的 `proc-start` 之后 heap 单调上升 | 该过程自身的定点迭代填满内存。看它的 CFG 大小（`logs` 里若有 `Skipped large procedure (…, size:N)` 说明另一些过程因超过 `--pulse-max-cfg-size` 被跳过） |
| 一串过程都“已开始未结束” | 是一条谁都没返回的调用链，峰值是各层之和，罪魁看每层 `proc-start` 之间的 heap 差 |
| heap 平稳但 `rss_kib` / `private_dirty_kib` 猛涨 | 不在托管堆：原生分配、序列化、SQLite 拷贝或压缩瞬态。看最近的 `payload-serialized` 与 `gc-compact-before/after` |
| `vm_size_kib` 远高于 `rss_kib` | 地址空间压力，通常是压缩预留了第二块堆大小的空间 |
| `summary_cache_entries` 持续增长、`summary_cache_reachable_words` 很大 | 跨过程的缓存滞留，`--summary-cache-max-heap-GB N` 能压住 |
| `summary_cache_entries` 很小（个位数）却 OOM | 与缓存无关，别在缓存上花时间——这正是上一版候选修复失效的原因 |
| 最后一条是 `instr-burst`，`heap_after - heap_before - child_growth` 很大 | 单条指令的自身分配很大，指令文本就在记录里 |
| 完全没有 `instr-burst` 却 OOM | 增长分散在大量小指令上，把阈值调低再跑：`INFER_HEAP_TRACE_INSTR_MB=8` |

## 3. 需要更细时的开关

| 环境变量 | 作用 | 代价 |
|---|---|---|
| `INFER_HEAP_TRACE=<前缀>` | 打开 JSONL 追踪 | 低 |
| `INFER_HEAP_TRACE_INSTR_MB=<n>` | 指令级突增阈值（默认 64 MB） | 调低会显著增加记录量 |
| `INFER_HEAP_TRACE_LEVEL=2` | 额外记录每次摘要缓存命中/未命中 | **很高**，只在怀疑缓存时用 |
| `INFER_HEAP_TRACE_SMAPS=1` | 记录 `private_dirty_kib`（读 `smaps_rollup`） | 高，内核要遍历页表 |
| `INFER_HEAP_TRACE_CACHE_WORDS=1` | 每条记录都测缓存可达字数 | **极高**，会遍历整个存活对象图；默认只在堆涨了 256 MB 时测一次 |
| `INFER_HEAP_TRACE_COMPACT=1` | 每个调度目标结束后强制压缩并记录压缩后的堆 | 中，用来看跨目标真正留存了多少 |
| `INFER_CLEAR_SUMMARY_CACHE_PER_PROC=1` | 每个顶层过程后清空摘要缓存 | 中。**判别性实验**：清了还死 ⇒ 与缓存无关；清了能活 ⇒ 是缓存滞留 |

## 4. 完整性必须单独核对

进程活下来、`report.json` 存在**都不等于分析完整**。跑：

```bash
python3 results/infer-oom/tools/check_completeness.py <results-dir> --infer <infer二进制> \
    [--files <changed-files-index>]
```

它列出每个已捕获且有定义、但没有 Pulse 摘要的过程，并归因为
`pulse-max-heap abort (explicit)` / `skipped: CFG larger than --pulse-max-cfg-size` /
`per-procedure --timeout` / `UNEXPLAINED`，最后给出 COMPLETE 或 INCOMPLETE。

注意两类**stock Infer 完全静默**的遗漏，它们不会出现在任何报告里，只有这个脚本能查出来：

- 每过程 CPU 超时（`--timeout`）：`logs` 里只有一行 debug 的 `TIMEOUT in pulse after …`；
- CFG 超过 `--pulse-max-cfg-size`：`logs` 里只有一行 `Skipped large procedure (…, size:N)`。

## 5. 上限该设多大

上限来自 `RLIMIT_AS`，比例由 `--max-heap-percent-of-address-space` 决定（默认 75，0 关闭）：
`ulimit -v 8388608`（8 GiB）⇒ 6 GiB 处放弃当前过程、4 GiB 处开始无损缓解。

- 机器内存宽裕时（例如 32 GB），把 `ulimit -v` 放到 16–20 GiB 比在勉强够用的上限下反复缓解更划算：
  实测在上限紧张时缓解会被反复触发而收效甚微。
- 上限不提供任何保证：过程要的比上限多时，没有本补丁是整轮崩溃，有本补丁是显式记录的单过程遗漏。
- 完全不设 `ulimit -v` 时机制惰性，需要显式给 `--pulse-max-heap`（单位是字）或
  `--summary-cache-max-heap-GB`。

## 6. 回归基线（本机实测，供对照）

| 场景 | 原版 | 本补丁 |
|---|---|---|
| Vim `ex_docmd.c`，`ulimit -v 8 GiB`，无追踪 | 715.6 s，堆 3.52 GB，48 条 | 720.6 s，堆 3.52 GB，48 条 |
| 同上但 CFG 上限放宽到 60000 | 127.8 s，堆 2.60 GB | 126.8 s，堆 2.60 GB |
| 单过程复现用例，`ulimit -v 2 GiB` | **崩溃**（47.4 s，无报告） | 37.4 s，有报告，2 个过程显式记录 |

内存宽裕时开销约 0.7%，结果逐条一致。
