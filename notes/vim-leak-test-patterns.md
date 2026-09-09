# 给 Vim 的泄漏修复写测试：三种模式

2026-09-09。来源：`vim/vim#21255` 被接受（`patch 9.2.1058`）过程中 yegappan 要求加测试，
以及 chrisbra 在我们的测试之上补的那一版。

这套做法背后的通用原理（为什么是"把裁决权收进测试内部"而不是"让它可观测"），
另见 [from-signal-to-assertion.md](from-signal-to-assertion.md)。

上游那 22 个同类泄漏修复**一个都没带测试**，所以"要不要测试"取决于 bug 能不能从脚本走到。
纯 `malloc` 失败路径的没人要求测；脚本可达的会被要求。

## 判据：这个泄漏有没有可断言的现象

按能观测到什么，分三种。**先跑一遍判据，再决定写哪种测试。**

```bash
# 未修复的二进制上跑触发脚本，看退出码
ASAN_OPTIONS=detect_leaks=1 ./vim -u NONE -N -i NONE -S trigger.vim
echo $?
```

| 退出码 | 说明 | 用哪种模式 |
|---|---|---|
| 非 0（1 或 134） | LSAN/ASAN 抓到了 | **模式 A** |
| 0，但脚本里有可见的计数差异 | LSAN 看不见（对象挂在全局链上） | **模式 B** |
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
不需要 ASAN 构建，普通 CI 就能跑——**比模式 A 更好，能用就优先用**。

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
