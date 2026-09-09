# 给 Vim 的泄漏修复写测试：三种模式

2026-09-09。来源：`vim/vim#21255` 被接受（`patch 9.2.1058`）过程中 yegappan 要求加测试，
以及 chrisbra 在我们的测试之上补的那一版。

背后的通用原理见 [from-signal-to-assertion.md](from-signal-to-assertion.md)：
先列出缺陷造成的所有异常现象——它们地位平等，都是要抓的 flag——再按准入和性价比筛。
下面三种模式就是 Vim 泄漏这一类里的三个候选。

上游那 22 个同类泄漏修复**一个都没带测试**，所以"要不要测试"取决于 bug 能不能从脚本走到。
纯 `malloc` 失败路径的没人要求测；脚本可达的会被要求。

## 选哪种模式：两道关卡

### 关卡一（准入）：这个现象在未修复的树上真的会翻吗

跟缺陷脱钩的现象再便宜也出局。`f_setmatches` 就有这么一个陷阱候选：

```vim
call setmatches([...])
call clearmatches()
call assert_equal([], getmatches())   " 比 test_refcount 还省事
```

**未修复也通过**——`clearmatches()` 确实把 match 列表清空了，缺陷只在于那几个
list 的引用没释放。任何候选都得先在未修复的树上跑一遍，看它翻不翻。

### 关卡二（性价比）：优先 A，A 不可用才退到 B

不是"B 便宜所以优先 B"。B 只在**价**上赢，A 在三件事上赢：

- **复用面**：`CheckAsan` 一次投入，后续所有内存 bug 都能复用；B 的断言是一次性的。
- **鲁棒性**：B 的断言里编码了"我认为泄漏表现为引用计数 +1"这个判断，
  我对缺陷的理解错了它可能照样通过；A 不需要我理解缺陷，直接观测非法内存行为。
  （本项目实证：`string_reduce` 那个 SUAR 的机制我解释错过两次。）
- **可审查性**：评审者看 `assert_equal(0, v:shell_error)` 一眼知道在测什么。

而且社区认 A——chrisbra 为它专门往 `util/check.vim` 加了共享 helper。

`#1 f_setmatches` 用 B，**不是因为 B 更好，是因为 A 在那个 case 不可用**。
（注：我们手上没有"两者都可用"的实例，上面是按成本结构判断的。）

### A 到底可不可用，跑一条命令

```bash
# 未修复的二进制上跑触发脚本，看退出码
ASAN_OPTIONS=detect_leaks=1 ./vim -u NONE -N -i NONE -S trigger.vim
echo $?
```

| 退出码 | 说明 | 结论 |
|---|---|---|
| 非 0（1 或 134） | LSAN/ASAN 抓到了 | **用模式 A** |
| 0，但脚本里有可断言的计数差异 | LSAN 看不见（对象挂在全局链上） | A 不可用，**退到模式 B** |
| 0，且没有任何脚本可见差异 | 只在分配失败时触发 | **模式 C**（多半不用写） |

## 模式 A：`CheckAsan` + `abort_on_error`（chrisbra 的写法）

适用：LSAN 能报的泄漏，以及 stack-use-after-return。
`#2 barline_parse`、`#3 string_reduce`、`#4 json_encode_lsp_msg`、`#7 ex_redir` 属于这一类。

chrisbra 为 `patch 9.2.1058` 新增了 `CheckAsan`（`src/testdir/util/check.vim`）：

```vim
" Command to check for running under ASAN
command CheckAsan call CheckAsan()
func CheckAsan()
  if execute('version') !~# '-fsanitize=[a-z,]*\<address\>'
    throw 'Skipped: requires an ASAN build'
  endif
endfunc
```

然后在子进程里跑触发脚本，断言退出码：

```vim
def Test_string_reduce_error_funccal()
  CheckAsan

  var save = $ASAN_OPTIONS
  defer setenv('ASAN_OPTIONS', save)
  $ASAN_OPTIONS = 'detect_stack_use_after_return=1:abort_on_error=1:' .. save

  var lines =<< trim END
      vim9script
      silent! echo reduce("abc", (acc, c) => [][0])
      qall!
  END
  writefile(lines, 'Xreducefc.vim', 'D')
  g:RunVim([], [], '-u NONE -S Xreducefc.vim')
  assert_equal(0, v:shell_error)
enddef
```

要点：

- `CheckAsan` 让非 ASAN 构建直接跳过，不会误报。
- `abort_on_error=1` 把 ASAN 的报告变成 SIGABRT，子进程退出码 134，于是能 `assert_equal(0, v:shell_error)`。
- **纯 LSAN 泄漏也行**：不加 `abort_on_error` 退出码就是 1，加了是 134，两者都非 0。实测
  `barline_parse` 的恶意 viminfo：未修复 exit=1 / 加 abort 后 134，修复后 0。
- `defer setenv(...)` 恢复环境变量，别污染后续测试。
- 触发脚本里的 `qall!` 位置会改变症状（见 `results/upstream-prs/redteam/logs/t3-exit-path-matrix.txt`），
  照抄验证过的那份，别自己改。

这比"只跑一遍路径、等 CI 捞 ASAN 日志"强：它在测试内部就断言了，本地也能跑。

## 模式 B：`test_refcount()`（LSAN 看不见时）

适用：泄漏的对象挂在全局链上（`list_T` 挂 `first_list`、`funccall_T` 挂 `current_funccal`），
LSAN 判定"可达"因而一声不吭。`#1 f_setmatches` 属于这一类，退出码两边都是 0。

关键观察：**泄漏的容器持有的是脚本里能拿到的对象**。容器不释放，这些对象的引用计数就降不回来，
而 `test_refcount()` 是 Vim 自带的内建函数，能直接断言。

```vim
" setmatches() with more than one "posN" entry used to keep a reference to
" every position list, so they were never released.
func Test_setmatches_pos_refcount()
  let p1 = [1, 1, 1]
  let p2 = [2, 1, 1]
  call assert_equal(1, test_refcount(p1))
  call assert_equal(1, test_refcount(p2))

  call setmatches([#{group: 'Search', priority: 10, id: 4, pos1: p1, pos2: p2}])
  call clearmatches()
  call assert_equal(1, test_refcount(p1))
  call assert_equal(1, test_refcount(p2))
endfunc
```

实测：未修复 `Expected 1 but got 2`（`clearmatches()` 之后仍是 2），修复后通过。
不需要 ASAN 构建、不需要子进程、不动全局状态，普通 CI 就能跑。
**但这是 A 不可用时的退路，不是更优解**——理由见上面关卡二。

单个 `posN` 的情况引用计数正常，可以作为对照写进同一个测试。

## 模式 C：只在分配失败时触发

`#5 vim9generics`、`#6 vim9class`、`#8 gui_gtk_x11` 属于这一类。
`test_alloc_fail()` 只对已经用 `alloc_id(size, aid_xxx)` 的调用点有效，这三处都是裸 `alloc()`，
要测就得先给上游加 `aid_*` 枚举值——改动比修复本身还大，不值得。

上游那 22 个全是这一类，也全都没带测试。**提交时不用主动加，被要求了再说明原因。**

## 跑测试时的两个坑（都踩过）

1. **`src/testdir/Makefile` 会给 `ASAN_OPTIONS` 追加后缀**。传
   `log_path=/tmp/asan` 实际落在 `/tmp/asan_test_vim9_builtin`；而 `detect_leaks=0` 会变成
   非法的 `detect_leaks=0_test_xxx` 直接让 ASAN 罢工。要传就传字符串型选项，例如
   `ASAN_OPTIONS=detect_leaks=0:log_path=/tmp/asanlog`。
2. **测试失败时 `.res` 文件根本不会生成**，不是"生成一个空的"。判断通过与否要看
   `make` 的退出码或 `messages` 文件，别用 `wc -c < xxx.res`——文件不存在会被当成 0 字节，
   把失败读成通过。
