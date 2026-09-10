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

## 2b. 定位到**哪条原语操作**把状态乘起来（新增）

`INFER_HEAP_TRACE` 只能告诉你哪个过程在涨。要回答"是分支、载入、字段读、调用、join 还是 widen
把抽象状态乘起来的"，加上 `INFER_HEAP_TRACE_OPS=1` 再跑一次：

```bash
INFER_HEAP_TRACE=<前缀> INFER_HEAP_TRACE_OPS=1 <guarded runner> …
python3 results/infer-oom/tools/analyze_ops.py <前缀>.<pid>.jsonl
```

三类记录：`op-start`（操作开始前）、`op`（成功结束后）、`op-error`（抛异常时）。
**`op-start` 是唯一能指认致命操作的记录**——`Fatal error: out of memory` 是运行时直接 abort，
不是异常，所以 try/with 和"成功后记录"都抓不到它。为控制体积，`op-start` 只在堆超过
`INFER_HEAP_TRACE_OPS_START_MB`（默认 512）之后才写。

每条记录的额外字段：

```
procedure     当前正在分析的过程（嵌套按需分析会正确恢复外层的名字）
kind          load | store | branch | call | metadata | widen
              前缀 node- 的是该 CFG 节点上所有 disjunct 的汇总，无前缀的是单个 disjunct
node          CFG 节点号
loc           源码位置（宏展开后的行列）
disjuncts_in  这条指令收到的 disjunct 数（Pulse 逐 disjunct 执行，通常是 1）
disjuncts_out 这条指令产出的 disjunct 数 —— 大于 1 就是它在乘状态
dropped       因 --pulse-max-disjuncts 被丢弃的数量
heap_before / heap_after   该操作前后的主堆字数
detail        指令文本（不含源码内容）
```

`analyze_ops.py` 首先判定**追踪停止时是否正好卡在某条操作里**，若是则直接打印该操作的
过程、类型、位置、节点、当时堆大小与指令文本——这就是致命操作。随后给出三张表：按操作类型汇总的总增长/最坏单次增长/disjunct 净变化；
按源码位置排序的总增长（带该位置最坏的那条指令文本）；以及**把 disjunct 乘得最多的前 10 条操作**。

这正是把 bisect 从"哪个宏"推进到"哪条原语"的工具：不需要再删代码，跑一次就能看到
是转换后的嵌套字段读、还是它后面的条件 prune，在把一个 disjunct 变成很多个。

代价：每条指令一条记录（本机夹具 61 310 条 / 10 秒），只在需要时开。

## 3. 需要更细时的开关

| 环境变量 | 作用 | 代价 |
|---|---|---|
| `INFER_HEAP_TRACE=<前缀>` | 打开 JSONL 追踪 | 低 |
| `INFER_HEAP_TRACE_INSTR_MB=<n>` | 指令级突增阈值（默认 64 MB） | 调低会显著增加记录量 |
| `INFER_HEAP_TRACE_OPS=1` | 每条抽象操作一条记录，含 disjunct 进出与丢弃数（见 §2b） | 中高，每条指令一条记录 |
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

## 4b. 已经排除的几种形状（省得重复试）

本机用通用夹具做过的对照，都在 `results/infer-oom/runs/` 里：

| 形状 | 峰值 | 结论 |
|---|---|---|
| 6–7 层调用链，共享对象 | 0.89 GB | 深度本身无效 |
| 6 层调用链，每个调用点独立子对象 | 0.26 GB | disjunct 上限把每个摘要卡住 |
| 单过程 1800 语句、CFG 1.4 万节点 | 2.54 GB | 有效，但那是 CFG 规模，不是过程内爆炸 |
| 宏密集、CFG 1.3 万节点（8 行源码） | 0.49 GB | **分支密度本身不是放大器** |
| 宏密集、CFG 83 万节点、放开 CFG 上限 | 2.32 GB | 放开上限也没爆 |
| 条件查找+间接取值+转换+3 层嵌套字段读（±写入、±循环），CFG 1 万节点 | 0.28–0.34 GB | 未复现 19/20 悬崖 |

也就是说：**光有条件查找、指针转换、嵌套字段读、写入和循环还不够**。还缺至少一个要素，
用 §2b 的操作级追踪在真实用例上跑一次，应该能直接指出它是什么。

另外一个副产品发现：宏密集代码非常容易越过 `--pulse-max-cfg-size`（570 行源码 → 83 万 CFG 节点）
而被**静默跳过**，那是召回损失而不是 OOM，只有 §4 的完整性脚本能查出来。

## 4c. 一个可以一次排除的候选（全局初始化器反复内联）

`Pulse.ml` 的 `set_global_astates` 会在每次载入全局常量时重新内联其初始化器，上游留着
`TODO: Initial global constants only once`。本机在三个 C 夹具上都**没有触发**它
（守卫 `is_global_constant` 依赖 C++ 的 `constexpr`/const 标记），所以很可能不是原因，
但排除它只要一次 grep：

```bash
grep -c global-init-inline <前缀>.<pid>.jsonl     # 0 表示该路径未触发，直接排除
```

若不为 0，再跑一次 `--no-pulse-inline-global-init` 对照；若 OOM 消失，
修复方向就是让每个全局的初始化器在一次过程分析内只内联一次。
该开关只是诊断用，关闭它会损失它本来提供的精度（剪掉不可行的 `全局 != 常量` 路径）。

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
