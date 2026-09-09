# 让含 `^` 的参数（所有锚定正则）原样传到 Infer 子进程

> 2026-09-09。对应请求文档 `/tmp/b2-obligation-recall/e1-infer-parser-oom-fix-request.md` 的 Problem 1。
> 补丁：`notes/infer-argfile-transport.patch`（74 行 diff，只改 `infer/src/base/CommandLineOption.ml`）。
> 验收测试：`results/infer-arg-models/verify_transport.sh`（17 项，新二进制 17/17，旧二进制 12/17）。
> Problem 2（Pulse OOM）按用户要求**没有做**。

## 1. 现象与真正的触发条件

Infer v1.2.0 在 `CommandLineOption.parse` 里把"要转交给子进程的参数"先用 `^` 拼成一个字符串
（`encode_argv_to_env`），拼的时候**整个丢掉**任何含 `^` 的参数并打印
`WARNING: Ignoring unsupported option containing '^' character`，随后再用 `^` 切回列表写进一个
argfile，把 `INFER_ARGS=@argfile` 交给子进程。所以每个锚定正则（`^\(my_alloc\)$`）在 argfile
里只剩一个悬空的选项名，效果取决于参数顺序：

| 正则后面是什么 | argfile 里的结果 | 子进程行为 |
|---|---|---|
| 另一个选项（`--pulse-model-alloc-pattern RE --pulse-model-free-arg-pattern F`） | `--pulse-model-alloc-pattern --pulse-model-free-arg-pattern`（F 也被丢） | 静默地把下一个选项名当成正则值，两个模型全部失效 |
| `--`（正则是最后一个选项） | 行尾悬空 | `option '--pulse-model-alloc-pattern' needs an argument`，make 模式 capture 失败，退出码 3 |

**哪些进程真的读 argfile**（用 `strace -f -e execve` 证实，`results/infer-arg-models/transport_test_before_fix.txt`）：

- 分析 worker 默认是 `fork(2)` 出来的（`--unix-fork` 默认为真，`ProcessPool.fork_child`），
  继承父进程已经解析好的 `Config`，**不重新解析**——这就是为什么 `infer analyze --jobs 2`
  在旧二进制上也能拿到正确正则、而请求文档里只在 `infer run` 下看到零报告；
- 被 `exec` 的进程才走 argfile：make/cc 模式下每次编译由 `lib/wrappers/gcc` 再拉起的 `infer`
  capture 进程、`--no-unix-fork` 时的分析 worker、Buck/xcodebuild 集成。
  旧二进制在 `--jobs 2 --no-unix-fork` 下报告为 0 条，正是模型在 exec 路径上丢失的直接证据。

## 2. 修法与取舍

`^` 字符串只是一个残留的中间表示：真正的传输格式早已是 argfile（一行一个参数），
`Filename.temp_file` + `Out_channel.output_lines`。所以修法是**把中间表示改成列表**：

```ocaml
let rev_args_to_export = ref [] in
let add_parsed_args_to_args_to_export () =
  ... let args = List.tl prog_args |> List.filter ~f:(Fn.non String.is_empty) in
  rev_args_to_export := List.rev_append args !rev_args_to_export
in
...
let argv_to_export = List.rev !rev_args_to_export in   (* 直接写 argfile *)
```

`encode_argv_to_env` 连同告警一起删除。没有选择的方向及原因：

- **可逆转义（如 `^^`）**：仍要改用户可见的 `INFER_ARGS` 语法契约（文档明确写着 `INFER_ARGS=--debug^--print-logs`），
  而且解决的只是一个本来就不该存在的中间层；
- **换分隔符**：只是把特殊字符换成另一个特殊字符；
- **前缀匹配**：请求明确禁止，而且 `verify_transport.sh` 的 decoy 用例专门防这个。

保留的东西：`env_var_sep = '^'` 只用于两处仍然合法的场景——切分用户手写的 `INFER_ARGS`，
以及 `add_to_env_args` 往 `@argfile` 后面追加 `--run-as-child N` 这类内部参数（它们不含 `^`）。
用户手写 `INFER_ARGS` 里若需要含 `^` 的值，用 `INFER_ARGS=@file`（测试覆盖）。

行为差异（都已在代码注释里写明）：空字符串参数原先也会被 `^` 编码吞掉，现在显式过滤，
因为 argfile 一行一个参数表示不了空串；含换行的参数在 argfile 格式下本来就不可表示，未改。

## 3. 构建

```bash
source tools/infer-env.sh && cd tools/infer-src && make -j8 opt     # 增量约 4 分钟
```

源码状态：`tools/infer-src` = v1.2.0（4c53e80）+ `infer-arg-models.patch` + `infer-argfile-transport.patch`，
`git diff --stat` 为 4 个文件。`scripts/build-patched-infer.sh` 第 6 步现在两个补丁都会应用，
第 8 步会同时跑 `verify_patch.sh` 和 `verify_transport.sh`。产物 `tools/infer-src/infer/bin/infer`，
版本串仍是 `v1.2.0-4c53e80`。

## 4. 验收（`results/infer-arg-models/verify_transport.sh`）

夹具 `transport_a.c`/`transport_b.c`：两个编译单元（保证 `--jobs 2` 真的有两个 worker），
allocator `my_alloc`、第二个候选 `other_alloc`、decoy `my_alloc2`（同前缀，锚定正则必须不匹配）、
第 2 参数释放函数 `my_free2`。测试正则 `^\(my_alloc\|other_alloc\)$` 和 `1:^\(my_free2\)$`
覆盖 `^ $ \( \|` 四种特殊字符。每次 `infer` 调用的输出都收集起来，最后统一检查没有任何 caret 告警。

| # | 检查 | 修前 | 修后 |
|---|---|---|---|
| 1 | `infer run --jobs 2`（make 模式：capture 走 exec 的 wrapper 子进程）退出 0，报告恰为 4 个泄漏 | PASS（靠参数顺序侥幸） | PASS |
| 2 | 正则作为最后一个选项 | **FAIL**：exit 3，`needs an argument`，无报告 | PASS |
| 3 | decoy `my_alloc2` 不被报告；把它加进正则后（阳性对照）被报告 | PASS | PASS |
| 4 | 原生 arg0 free 模型误释放 ctx，arg-position 模型才正确 | PASS | PASS |
| 5 | `infer analyze --jobs 1` / `--jobs 2`（fork） | PASS | PASS |
| 6 | `infer analyze --jobs 2 --no-unix-fork`（exec 的 worker） | **FAIL**：0 条报告 | PASS |
| 7 | `--debug` 保留的全部 argfile（含子进程 `@父argfile` 引用展开）里两个正则逐字节一致，且至少一个由 `--run-as-child` worker 写出 | **FAIL** | PASS：5 个 argfile 全部一致，2 个来自 worker |
| 8 | `INFER_ARGS='--pulse-only^--jobs^2'` 仍生效，并与命令行模型叠加 | PASS | PASS |
| 9 | 显式 `@argfile`（命令行、`INFER_ARGS=@file`）与 `.inferconfig` | PASS | PASS |
| 10 | 全部运行无 `Ignoring unsupported option containing '^'` 告警 | **FAIL**：32 行 | PASS |

完整输出：`results/infer-arg-models/transport_test_before_fix.txt`（12/17）与
`transport_test_after_fix.txt`（17/17）。原有 `verify_patch.sh` 在新二进制上 12/12。

## 5. 对流水线的意义

`memhint/analyzers/infer.py` 的 `anchored`/`anchored-argn` 模式一直在往 `infer analyze` 传锚定正则；
因为 worker 是 fork 出来的，Vim 上的结果并没有受影响。受影响的是所有 exec 路径：
make 模式 `infer run`、`--no-unix-fork`、Buck/xcodebuild。此后任何模式都不再需要 `@args` 或
`.inferconfig` 绕路。
