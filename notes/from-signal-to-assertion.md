# 怎么让一个失败真的失败

起因：`vim/vim#21255` 里 chrisbra 把我们的测试重写了一遍。我们那版是"跑一遍出问题的
路径，然后等 CI 去 ASAN 日志里捞"，他那版是这样：

```vim
def Test_string_reduce_error_funccal()
  CheckAsan                                         " 不是 ASAN 构建就跳过

  var save = $ASAN_OPTIONS
  defer setenv('ASAN_OPTIONS', save)                " 别污染后面的测试
  $ASAN_OPTIONS = 'detect_stack_use_after_return=1:abort_on_error=1:' .. save

  var lines =<< trim END
      vim9script
      silent! echo reduce("abc", (acc, c) => [][0])
      qall!
  END
  writefile(lines, 'Xreducefc.vim', 'D')
  g:RunVim([], [], '-u NONE -S Xreducefc.vim')      " 子进程
  assert_equal(0, v:shell_error)                    " 断言
enddef
```

`abort_on_error=1` 把 ASAN 那份"打印完继续跑"的报告变成 SIGABRT，子进程于是退出 134，
父进程读到一个普通整数，就能断言了。测试自己会红，本地就能跑，不用等 CI 捞日志。

## 一般化

这里没有什么"缺陷本身"。`string_reduce` 那个 bug 是「`funccall_T` 滞留在
`current_funccal` 链上」，ASAN 那份报告只是它的一个下游后果，`test_refcount()` 读到的
引用计数是另一个。**它们地位平等，都只是我们要抓的 flag。**

所以做法就是：**有什么，判断一下，选一个。** 柿子找软的捏——既然都能抓到同一个缺陷，
那就挑最省事的。选中的那个代码读不到，才去搭转换链（升级成致命 + 开子进程 + 读退出码）。

我们这轮踩过两个坑，留神一下：

**未修复也通过的，一毛钱关系都没有。** `f_setmatches` 有个比引用计数更省事的候选，
`clearmatches()` 之后 `assert_equal([], getmatches())`——但未修复也是 `[]`，
因为 match 列表确实被清空了，缺陷只在于那几个 list 的引用没释放。捏之前先在
未修复的树上跑一遍，它必须翻。

**断言里编码了"我对缺陷的理解"的，要打折。** 引用计数断言写的是"我认为这个泄漏
表现为引用计数 +1"——我理解错了它可能照样通过。这不是假设：`string_reduce` 那个
stack-use-after-return 的机制我解释错过两次，最后靠 `save_funccal`/`restore_funccal`
才纠正过来。ASAN 不管我怎么解释，它只看内存。

## 观测手段缺席时，它是跳过还是撒谎

`CheckAsan` 那一行不是细节。没有它，在非 ASAN 构建上缺陷照样在、进程照样退 0，
`assert_equal(0, v:shell_error)` **就通过了**——绿灯，让你以为这个 bug 被守住了。

同款我这轮踩了两次：

- `verify_patch.sh` 里有一项是"期望报告数为 0"，而未打补丁的 Infer 遇到新选项直接
  报 unknown option 退出、根本不生成报告文件——于是它以完全错误的理由通过。
- Vim 的失败测试**不写 `.res` 文件**，我用 `wc -c < test_match.res` 判断，
  文件不存在被读成 0 字节 = 通过，于是我报告"两棵树都通过"，而未修复那棵正按设计失败。

写完断言问一句：要观测的东西根本没运行的话，这个测试是什么颜色？答案得是跳过或红。

## 换个领域也一样

大多数运行时诊断默认都是"报告完继续跑"，第一步都是去找那个把它变致命的开关：

| | 默认信号 | 变致命 | 断言对象 |
|---|---|---|---|
| ASan/UBSan/TSan | stderr 报告 | `halt_on_error=1` / `abort_on_error=1` | 子进程退出码 |
| LeakSanitizer | 退出时报告 | 默认已非 0（ASan 内置 1，独立 LSan 23） | 子进程退出码 |
| 编译器警告 | 编译日志 | `-Werror` | 编译成不成功 |
| Python 警告 | stderr | `-W error` / pytest `filterwarnings = error` | 抛不抛异常 |
| Node 未处理 Promise | 警告 | `--unhandled-rejections=throw`（新版默认） | 退出码 |
| Go goroutine 泄漏 | 无 | `goleak.VerifyTestMain` | goleak 自己断言 |
| 日志里的 ERROR | 日志文件 | 无——改用捕获 handler | 捕获到几条 |
| SQL N+1 | 只是慢 | 无——自己数 query | 计数 |
| fd 泄漏 | 无 | 无——前后各数一次 | 前后相等 |

后面几行说明**不是所有信号都有"变致命"的开关**。没有的时候就自己造一个进程内的
可观测量（计数器、前后快照），这反而更省事——不用子进程，也不用守卫。

Vim 泄漏这一类的具体落地见 [vim-leak-test-patterns.md](vim-leak-test-patterns.md)。
