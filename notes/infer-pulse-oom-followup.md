# Pulse 单 worker OOM（续）：真正的失败相位、最小复现与可达的内存上限

> 2026-09-09。对应跟进请求 `/tmp/e2-infer-pulse-oom-followup-request.md`。
> 前一轮：`notes/infer-pulse-oom.md`（摘要缓存驱逐）——**在目标压力用例上无效**，原因见 §1。
> 本轮补丁：`notes/infer-pulse-oom-followup.patch`。工作目录（gitignored）：`output/oom/`。

## 0. 先说结论

1. **上一轮的候选修复没有解决目标压力用例，本轮确认了原因。** 那一版只在
   `Summary.OnDisk.add` 里驱逐摘要缓存，而目标用例是在**一次尚未完成的过程分析期间**耗尽内存的
   （可能是一个大过程，也可能是一条谁都没返回的调用链——本机复现属于前者，追踪里“已开始且从未
   结束”的过程恰好只有 1 个）：
   期间既不会调用 `add`，缓存里也几乎没有东西可驱逐（本机复现时全程只有 **3 条**缓存条目）。
   对方观察到的“无驱逐日志、无 `instr-burst`、过程只有 start 没有 end”三条现象，本机全部重现并解释。
2. **最小通用复现已隔离**：一个 C 过程，约 1800 条语句、CFG 约 1.4 万节点（在默认
   `--pulse-max-cfg-size 15000` 之下）、约 1800 个互不相同的内存单元、每 40 条语句一个二路分支。
   单 worker、`--timeout 60`、`ulimit -v 2097152`（2 GiB）下，原版 Infer 在 **47.4 秒**时
   `Fatal error: out of memory`（SIGABRT，退出码 134），峰值 RSS 1.93 GB，无 `report.json`。
   生成器 `results/infer-oom/tools/gen_wide.py`，确定性，无外部依赖。
3. **根因（已证）**：Pulse 的定点迭代把**每个 CFG 节点**的前置与后置状态都保存在不变式映射里，
   每个状态是最多 `--pulse-max-disjuncts`（默认 20）个 disjunct 的列表。单个过程的峰值内存因此是
   `节点数 × disjunct 数 × 单状态大小`，全部同时存活，发生在**任何摘要被构建或写库之前**；
   按需分析是嵌套的，所以一般情况下峰值是调用栈上所有未完成过程的这个乘积之和。
   默认的 `--pulse-max-cfg-size 15000` 允许的过程规模，已经远超这个乘积撑爆地址空间的临界点。
   结构化追踪显示堆在 39 秒内从 0.01 GiB 单调涨到 1.65 GiB，增量来自**普通指令**
   （`*&acc:=…`、`*n$3877.f246:=…`，每条 60–200 MB），没有任何单次巨量分配。
4. **v1.2.0 里唯一的内存防线够不着这个相位**：`--pulse-max-heap` 全仓只有一个检查点，在
   `PulseTransferFunctions.exec_instr` 的开头；一条调用指令内部逐个应用被调摘要的循环、逐 disjunct
   的执行循环、widen、过程摘要构建、摘要序列化，全都没有内存检查。摘要缓存则完全无界
   （`--summaries-caches-max-size` 这个选项在本版本里**没有任何代码读取**）。
5. **本轮修复**：新增 `base/MemoryPressure.ml`，一个可以从分析内层循环调用的廉价检查点，
   接到上述 5 个此前不可达的位置；它先做**无损**缓解（驱逐库里已有的缓存摘要 + `full_major`，
   必要时 compact），仍然超限才让 Pulse **显式放弃当前过程并记录**。上限按
   `--max-heap-percent-of-address-space`（默认 75）取自 `RLIMIT_AS`，并且是拿**实际占用的地址空间**
   （`/proc/self/statm`）比对，因为致命的是地址空间而不是托管堆。
6. **诚实的边界**：在一个过程本来就装不下的上限里，没有任何配置能完整分析它。修复把
   “整轮崩溃、零报告”换成“**完整报告 + 显式记录的单过程遗漏 + INCOMPLETE 判定**”。
   真正体现无损价值的是内存够用的场景（Vim 大 TU，见 §5）。

## 1. 为什么上一轮的修复无效（已证）

`Summary.OnDisk.evict_if_heap_too_big` 只在 `add` 里被调用，而 `add` 只在
`store`、`reset`、`load_summary_to_spec_table` 三处发生——都在**过程与过程之间**。
目标失败发生在一个过程内部，于是：

- 对方的候选运行没有任何驱逐日志：`add` 从未被调用；
- 本机复现的结构化追踪显示，整个失败过程期间 `summary_cache_entries` 始终是 **3**：
  即使驱逐真的发生，也没有东西可放；
- 对方的 `instr-burst` 为 0：那条追踪只在指令**正常返回后**记录，且默认阈值 64 MB；
  本机同一形态的失败里单条指令自身增长 60–200 MB，能记录到 9 条，但它们只是症状不是原因。

## 2. 最小通用复现

```bash
python3 results/infer-oom/tools/gen_wide.py --out output/oom/corpus/wide2 \
    --stmts 1800 --branch-every 40 --cells 600 --bigprocs 2
cd output/oom/corpus/wide2 && infer capture --results-dir infer-cap -- make && make clean
VMEM_KB=2097152 WALL=900 INFER=<控制版> \
    results/infer-oom/tools/run_guarded.sh wide2-cap2-control output/oom/corpus/wide2/infer-cap
```

| 项目 | 值 |
|---|---|
| 源文件 | `w0.c` / `w1.c` / `wide.h`，共 12 341 行，2 个大过程 + 2 个辅助过程 |
| capture | 2.4 MB，4 个过程（全部有定义） |
| Infer | v1.2.0-4c53e80，源码树 `tools/infer-src`，`make -j8 opt` |
| 分析参数 | `analyze --pulse-only --jobs 1 --max-jobs 1 --timeout 60` |
| 限制 | `ulimit -v 2097152`（2 GiB）、900 s 进程组 wall guard、`nice -n 10`、`ionice -c 3` |
| 失败信号 | `Fatal error: out of memory`，SIGABRT，退出码 134 |
| 时间/内存 | 47.4 s；峰值 RSS 1.93 GB；追踪最后一刻 heap 1.65 GiB / RSS 1.55 GiB / VM 1.82 GiB |
| 失败范围 | 一次未完成的过程分析期间：追踪里“已开始且从未结束”的过程只有 `bigproc0` 一个，它的被调过程都已正常结束；非跨过程、非跨文件累积 |

**最小化过程（每一步都实测）**：

| 形态 | 峰值 RSS | 结论 |
|---|---|---|
| 6–7 层调用链，共享对象（`deep2`, 16 过程） | 0.89 GB | 深度本身无效：各层写同一批单元 |
| 6 层调用链，每个调用点独立子对象（`deep3`, 13 过程） | 0.26 GB | 仍无效：`--pulse-max-disjuncts 20` 把每个摘要卡住，丢弃 1294 个 disjunct |
| 4 TU × 20 过程、分支/别名/函数指针（`c5`, 739 过程） | 3.03 GB | 有效但属于**跨过程累积**，缓存驱逐能压住（上一轮已验证） |
| 单过程 6000 语句（`wide1`） | 0.13 GB | 无效：CFG 43 784 > `--pulse-max-cfg-size`，**被静默跳过** |
| **单过程 1800 语句、CFG ≈ 1.4 万节点（`wide2`）** | **2.54 GB（无限制时）** | **有效**：这是复现用例 |
| 单过程 1500 语句 + 大足迹被调（`wide3`） | 1.06 GB | 更弱：语句数减少即 CFG 减小 |

去掉任一要素都不再复现：语句数降到使 CFG 明显小于 1.4 万 → 峰值线性下降；
语句数升到 CFG 超过 15000 → 过程被跳过，峰值降到 0.13 GB。

## 3. 根因、假设与未决

**已证事实（源码 + 测量）**

- `--pulse-max-heap` 的检查点只有 `Pulse.ml:1546` 一处，在指令边界。
- 细粒度的 `Timer.check_timeout ()` 有 3 处，其中 `PulseCallOperations.ml:482`（一条调用指令内
  逐个应用被调摘要）与 `AbstractInterpreter.ml:418`（逐 disjunct）都**没有**内存检查；
  `Domain.widen`、`PulseSummary.of_posts`、`Summary.OnDisk.store` 的序列化同样没有。
- `Summary.OnDisk` 的缓存无界；`--summaries-caches-max-size` 在本版本无人读取。
- 失败发生在单过程内：追踪里 `bigproc0` 只有 start；缓存条目全程 3 条；无 8 MB 以上的序列化负载。
- 堆单调增长、无单次巨量分配；死亡时 VM > 堆 > RSS，符合地址空间耗尽。
- `InferAnalyze.ml` 的压缩阈值单位错误（上一轮已修）：日志的 “heap size= N GB” 实为 N Gbit。

**假设（有间接证据，未在本机证明）**

- 对方那个失败 TU 与 `wide2` 同型：一个 CFG 接近 15000 节点、状态更丰富的过程；
  RSS 7.13 GiB 对 8 GiB 上限，与本机 1.93 GB 对 2 GiB 上限比例一致。
- 原始报告里 `--pulse-max-heap 200000000`（1.6 GB）挡不住 OOM：本机机制下这个阈值**会**触发，
  除非增长发生在两次指令边界之间（本轮新增的检查点正是覆盖这一段）。需要对方用新诊断复核。

**未决**

- 单过程状态的真实上限与 `--pulse-max-cfg-size` 之间没有任何一致性检查：15000 这个默认值
  并不保证任何内存上界，这是设计缺口而不只是实现缺陷。
- 超时（`--timeout 60`，CPU 时间）导致的遗漏在 stock Infer 里**完全静默**（只有 debug 日志），
  本轮没有改这一点，只是由完整性脚本查出来。

## 4. 修复

补丁在上一轮 `notes/infer-pulse-oom.patch` 之上。核心是一个**可以从分析内层循环调用**的检查点，
`base/MemoryPressure.ml`：

| 位置 | 此前 | 现在 |
|---|---|---|
| `PulseTransferFunctions.exec_instr`（指令边界） | 唯一的 `--pulse-max-heap` 检查 | 复用它已测的堆值调用 `check_with_heap`，频率与上游相同 |
| `PulseCallOperations.call_aux`（一条调用指令内逐个应用被调摘要） | 只有超时检查 | 采样检查点 |
| `AbstractInterpreter.exec_instr`（逐 disjunct） | 只有超时检查 | 采样检查点（只缓解，不放弃：其它 checker 不知道如何记录遗漏） |
| `Domain.widen`（循环头） | 无 | 采样检查点 |
| `PulseSummary.of_posts`（过程摘要构建） | 无 | 采样检查点 |
| `Summary.OnDisk.store`（序列化 + 写库） | 无 | 序列化前缓解一次 + 前后各一个追踪检查点 |

行为分两级：

1. **无损缓解**：驱逐库里已有的缓存摘要（`Summary.OnDisk.evict_half`），`Gc.full_major`，
   必要时 `Gc.compact`。摘要在入缓存前已写进 `results.db`，被驱逐的下次使用时读回，结果不变。
2. **显式放弃**：仍然超限时抛 `MemoryPressure.OverHardLimit`，由 Pulse 的 `checker` 捕获，
   该过程无摘要，并写入 `<results-dir>/pulse/oom-aborted-procedures-<pid>.txt`：
   过程唯一名、文件、行号、当时堆大小、**该过程自身造成的增长**。最后一项用来区分
   “这个过程自己涨上去的”和“它只是在别人已经涨满时被启动”——因为异常被最内层的
   `checker` 接住，被放弃的未必是罪魁。

上限取自 `RLIMIT_AS`（`--max-heap-percent-of-address-space`，默认 75），并且比对的是
**实际占用的地址空间**（`/proc/self/statm`），不是托管堆——致命的是地址空间，它还包含压缩瞬态、
原生分配和 SQLite 对序列化摘要的拷贝。地址空间无限制时（通常情况）整套机制完全惰性。

**两处必要的代价控制**，都是实测逼出来的：

- `/proc/self/statm` 的读取只按调用计数限流。最初还额外在“堆超过水位线时”强制读取，
  结果变成每条 Pulse 指令一次系统调用：同一个 Vim TU 从 715.6 s 变成 1362.6 s（+90%）。
- 无效缓解指数退避。当一个过程自身的活状态填满内存时，缓存里没有东西可驱逐、full_major 也
  释放不了什么（实测第一次缓解释放 0 字节）；每 15 秒重复一次会吃掉运行的一大部分。
  释放不足十分之一就把间隔翻倍，上限 10 分钟；释放有效则复位。压缩同理：连续两次缩不动就停用。

## 5. 验证

全部运行：单 worker（`--jobs 1 --max-jobs 1`）、`--pulse-only`、`--timeout 60`、`nice -n 10`、
`ionice -c 3`、进程组 wall guard、结果目录每次重建、启动前检查
`MemAvailable > 12 GiB` / 负载 ≤ 2 / 暂存盘 ≥ 50 GiB / 无其它 Infer 进程。
主机全程 `MemAvailable` ≥ 18 GiB、`SwapFree` 不变、负载 ≤ 1.5、`PRAGMA integrity_check = ok`。

### 5.1 复现用例（`wide2`，2 GiB 地址空间）

| | 原版 | 本补丁 |
|---|---|---|
| 结果 | `Fatal error: out of memory`，SIGABRT，退出码 134 | 退出码 0 |
| wall | 47.4 s（崩溃） | 71.5 s |
| 峰值 RSS | 1.93 GB | 1.49 GB |
| `report.json` | **无** | 有 |
| 完整性判定 | 无从判定 | INCOMPLETE，2 个过程，全部显式归因 |

修复版的 `logs` 现在自带诊断，不需要开追踪：

```
Memory: address space limited to 2.00 GB (RLIMIT_AS); relieving memory pressure above 1.00 GB,
        giving up on the current procedure above 1.50 GB
Memory: 53% / 76% / 97% of the ceiling in use (…)
Memory: peak managed heap 1.50 GB, peak address space 1.51 GB, 2 relief(s), 2 procedure(s) given up on
```

`pulse/oom-aborted-procedures-<pid>.txt`：

```
bigproc0  w0.c  18  heap_words=201838080  heap_words_grown_by_this_procedure=200521728
bigproc1  w1.c   3  heap_words=180560896  heap_words_grown_by_this_procedure=176414720
```

最后一列说明这两个过程正是自己把内存撑满的，不是被牵连的旁观者。

### 5.2 内存宽裕时的开销与等价性

| 夹具 / 上限 | 原版 | 本补丁 | 差异 |
|---|---|---|---|
| Vim `ex_docmd.c`，8 GiB | 715.6 s，堆 3.52 GB，48 条 | 720.6 s，堆 3.52 GB，48 条 | **+0.7%**，报告逐条一致 |
| `wide1` + `--pulse-max-cfg-size 60000`，8 GiB | 127.8 s，堆 2.596 GB | 126.8 s，堆 2.596 GB | 无差异 |
| `wide1` 同上，12 GiB | — | 127.8 s，堆 2.596 GB | 与 8 GiB 相同 |

第二、三行同时回答了“把 `ulimit -v` 放宽是否有用”：上限来自 `RLIMIT_AS`，放宽即等比抬高上限，
够用时机制完全惰性，耗时与内存与原版一致。

### 5.3 回归

- `results/infer-arg-models/verify_patch.sh`：**12 passed, 0 failed**
- `results/infer-arg-models/verify_transport.sh`：**17 passed, 0 failed**

第一个问题（含 `^` 的锚定正则原样传到子进程）的行为未受影响。

### 5.4 补丁与构建

`notes/infer-pulse-oom-followup.patch`（16 个文件，1236 行）在 v1.2.0-4c53e80 上依次应用
`infer-arg-models` → `infer-argfile-transport` → `infer-pulse-oom` → 本补丁后干净应用，
并已核对**精确还原**本次测量所用的构建树。构建命令：
`source tools/infer-env.sh && cd tools/infer-src && make -j8 opt`。

## 5b. 对方环境的结构性发现与本机的对照（2026-09-09 晚）

对方在真实用例上完成了受控 bisect，结论与本机的通用夹具**部分吻合、部分不同**，值得记下：

吻合的部分——失败在**一次未完成的过程分析期间**，关掉过程间分析仍然发生，摘要缓存条目很少，
所以上一轮的缓存驱逐够不着。这三点本机都独立复现了。

不同的部分——他们的目标过程只有 **221 行**，不是 CFG 巨大；触发点是一个宏展开，
包含条件查找、间接参数取值、指针转换成控制结构、嵌套字段读、以及随后的条件与 join，
把它换成标量赋值就降到 0.6 GiB。最有信息量的是 **disjunct 上限的悬崖**：
1/5/10/15/18/19 全部完成，默认的 20 就 OOM。

本机按这个结构做了三版通用夹具（基础版、加通过转换指针的写入、再加事件链表循环），
CFG 都控制在 1 万节点、过程确实被分析（摘要 302 KB），但峰值只有 0.28–0.34 GB，
20 与 19 没有任何差别。**没有复现那个悬崖**。另外单独验证了两件事：
宏密集但只有分支密度时（CFG 1.3 万节点，8 行源码）峰值 0.49 GB；
把 CFG 上限放开让 83 万节点的宏密集过程真的被分析，峰值也只有 2.32 GB。
所以**分支密度本身不是放大器**——`--pulse-max-disjuncts` 会把它截断。

结论：光有"条件查找 + 指针转换 + 嵌套字段读 + 写入 + 循环"还不足以引爆，还缺至少一个要素。
本轮因此把力气放在让对方能在真实用例上一次定位到**哪条原语操作**，见 §5c。

## 5c. 操作级诊断（本轮新增）

`INFER_HEAP_TRACE_OPS=1` 时，每条抽象操作产生一条 JSONL 记录：操作类型
（load / store / branch / call / metadata / widen）、源码位置、**该指令收到与产出的 disjunct 数**、
被 `--pulse-max-disjuncts` 丢弃的数量、以及操作前后的主堆字数；widen 另外记录迭代轮次。
`results/infer-oom/tools/analyze_ops.py` 把它汇总成三张表，其中一张直接列出
**把 disjunct 乘得最多的前 10 条操作**。

这把 bisect 的粒度从"哪个宏"推进到"哪条原语操作"，不需要再删代码。
本机夹具上验证：10 秒的运行产生 61 310 条记录，覆盖 20 569 次 load、10 250 次 branch、
4 364 次 store、3 次 widen。用法见 `notes/infer-oom-diagnostics-howto.md` §2b。

## 5d. 操作追踪的三处修正（对方反馈后）

对方在真实用例上跑了 19 与 20 两次带操作追踪的分析，指出三处限制，已全部修正：

| 限制 | 修正 |
|---|---|
| 记录里 `procedure` 为空 | 按需分析维护一个过程名栈，嵌套返回时恢复外层名字；每条记录带 `procedure` |
| `node` 恒为 `-` | 用 `CFG.Node.pp_id` 记录 CFG 节点号；逐 disjunct 的记录与节点级汇总分别标记（`load` 对 `node-load`） |
| 只在操作成功后记录，抓不到致命那一步 | 新增 `op-start` 与 `op-error`。**关键**：`Fatal error: out of memory` 是运行时 abort 而非异常，try/with 抓不到，只有操作**开始前**的记录能指认它。`op-start` 在堆超过 `INFER_HEAP_TRACE_OPS_START_MB`（默认 512）后才写，避免体积翻倍 |

`analyze_ops.py` 相应升级：先判定追踪停止时是否卡在某条操作里，若是则直接打印该操作的过程、
类型、位置、节点、当时堆大小与指令文本。该脚本此前因 `.gitignore` 的 `tools/` 规则**未能入库**
（`results/infer-oom/tools/` 下 16 个脚本全部被忽略），已加例外规则修正。

**他们的关键数据**：真实用例中最大的堆增长点是一条**嵌套字段 load**，`disjuncts_in=1`、
`disjuncts_out=1`，单次操作增长达 0.95 GiB。另有一处更早的调用点在 19 与 20 两种配置下分别
产出 19 与 20 个后继状态，是目前唯一的 fan-out 线索，但因果关系未证。

**本机对这条 load 的一个候选解释（未证实）**：`Pulse.ml` 的 `Load` 分支里，
`set_global_astates` 会在**每次载入全局常量或全局函数指针时重新内联其初始化器**
（`dispatch_call` 到 `__infer_globals_initializer_*`），上游还留着
`TODO: Initial global constants only once`。一张大的全局事件/调度表因此可能在每次被读到时
重新物化进抽象状态。但它只匹配 `Lvar pvar`（整体载入全局变量），本机用 `table[i].v0`
（`Lindex`）构造的夹具走不到该路径，峰值仅 0.31 GB，**假设未证实**。
用新的 `op-start` 记录里的 `detail` 可以直接判定：若那条 load 的指令文本形如
`n$X=*&<全局名>`，即走的这条路径。

## 6. 遗留阻塞

1. **目标压力用例本身仍未在本机复现。** 本机最大的通用夹具在 8 GiB 上限下不会越界；
   复现是靠把上限降到与夹具峰值同量级。对方环境的复核方法见
   `notes/infer-oom-diagnostics-howto.md`。
2. **上限紧张时代价明显。** 当一个 TU 的正常峰值接近上限时（例如 Vim `ex_docmd.c` 需要 3.5 GB
   而上限 4 GiB），缓解会被反复触发、过程会被放弃，运行时间显著变长。建议把 `ulimit -v` 放到
   机器承受得起的水平，让本机制只作兜底，而不是当作常规限流手段。
3. **`--pulse-max-cfg-size` 与内存之间没有一致性保证。** 默认 15000 既不保证内存上界，
   超过它又是**静默跳过**（只有 `logs` 里一行 internal error）。本轮没有改这个默认值，
   只是让完整性脚本把这类遗漏列出来。真正的修法是让 CFG 上限与内存预算挂钩，属于上游设计问题。
4. **超时遗漏仍然静默。** `--timeout`（CPU 时间）导致的过程遗漏在 stock Infer 里只有一行 debug 日志，
   报告里没有任何痕迹。本轮同样只是让完整性脚本查出来，没有改 Infer 的行为。
5. **对方那个 19/20 disjunct 悬崖没有在通用夹具上复现**，触发它的第三个要素未知。
   这是当前最重要的未决问题：在真实用例上用 §5c 的操作级追踪跑一次，
   对比 `--pulse-max-disjuncts 19` 与 `20` 两份记录，差异应当直接指出是哪条操作。
6. **原始报告里 `--pulse-max-heap 200000000` 挡不住 OOM 的现象未复现。** 本机机制下该阈值会触发。
   需要对方用新诊断复核：如果新版仍然直接崩溃，`Memory:` 三行日志与 JSONL 追踪能立刻区分
   “检查点没触发”“触发了但内存不在托管堆”“单次分配跨过了两个检查点”这三种可能。
