# django__django-14771 Mutation 分析

## 问题背景

Django 的自动重载器 (`django/utils/autoreload.py`) 在检测到代码变更时会用 `get_child_arguments()` 重建子进程的启动命令行。原实现只把 `sys.warnoptions`（`-W` 警告选项）传递给子进程，却遗漏了 `sys._xoptions`（即 `-X` 实现专属选项，如 `-Xutf8`、`-Xa=b`）。结果是：用 `python -X utf8 manage.py runserver` 启动时，重载后的子进程丢失了 `-X` 选项，行为与父进程不一致。

## Golden Patch 语义分析

补丁在 `args` 构建后新增一段：

```python
if sys.implementation.name == 'cpython':
    args.extend(
        f'-X{key}' if value is True else f'-X{key}={value}'
        for key, value in sys._xoptions.items()
    )
```

关键语义点：
1. **平台守卫** `sys.implementation.name == 'cpython'`：`-X` 是 CPython 专属，PyPy 等不支持，因此仅在 CPython 上注入。
2. **遍历** `sys._xoptions.items()`：键为选项名，值为选项值。
3. **布尔标志 vs 带值选项**：当 `value is True`（纯开关，如 `-Xutf8`）输出 `-X{key}`；否则输出 `-X{key}={value}`（如 `-Xa=b`）。
4. **追加方式** `args.extend(...)`：把生成器逐项拼接进 `args`。

## 调用链分析

`restart_with_reloader()` → `get_child_arguments()` → `subprocess.run(args, ...)`。`get_child_arguments()` 的返回列表直接作为子进程命令行。测试 `tests/utils_tests/test_autoreload.py::TestChildArguments` 通过 mock `sys._xoptions` 等全局状态，断言返回的 args 列表精确相等。新增的 F2P 测试 `test_xoptions` mock `{'utf8': True, 'a': 'b'}`，期望 `[..., '-Xutf8', '-Xa=b', ...]`，是唯一直接覆盖新代码语义的测试；其余测试 mock `sys._xoptions={}` 以保持原有断言不变（P2P）。

## 替换决策总览

| 槽位 | 原始 mutation | 分类 | 决策 | 新 strategy_code | 失败的 F2P |
|------|--------------|------|------|-----------------|-----------|
| A | `'cpython'`→`'pypy'` | 🟢 保留 | KEEP | A1 | test_xoptions |
| B | `==`→`!=` | 🔴 与 A 在 CPython 上功能等价 | REPLACE | A2 | test_xoptions |
| C | 丢弃 `={value}` | 🟢 保留 | KEEP | C1 | test_xoptions |
| D | 与 A 字节相同 | 🔴 重复 | REPLACE | B3 | test_xoptions |
| E | 与 B 字节相同 | 🔴 重复 | REPLACE | C3 | test_xoptions |

## 各组 Mutation 分析

- **A（保留）**：把守卫平台名改成 `'pypy'`，整段在 CPython 上被跳过，`-X` 选项全部丢失。形态自然（像写错平台名），保留。
- **B（替换）**：原 `==`→`!=` 在 CPython 下与 A 一样使整段被跳过，失败模式与 A 完全重合，属功能等价冗余，替换。
- **C（保留）**：丢掉 `={value}`，使带值选项退化为纯开关。目标是 else 分支，与其它槽位正交，保留。
- **D（替换）**：原与 A 字节相同的重复 diff，替换。
- **E（替换）**：原与 B 字节相同的重复 diff，替换。

## 新设计 Mutation 说明

为最大化失败模式正交性，5 个 mutation 分别打击新代码的不同语义维度，且全部仅令 `test_xoptions` 失败、不破坏 P2P：

- **B → A2（参数/顺序）**：`for key, value` 改为 `for value, key`，解包顺序错位。代码仍能运行，但输出变成 `-XTrue=utf8`、`-Xb=a`，键值被转置——典型的解包顺序笔误。
- **D → B3（布尔/比较反转）**：`value is True` 反转为 `value is False`，使开关型选项错误地走 else 分支，输出 `-Xutf8=True`。模拟真值判断方向写反。
- **E → C3（文本/分隔符编码）**：用 `:` 替代 `=` 作为键值分隔符，输出 `-Xa:b`。形态完全合法（像误用了另一种 CLI 约定），仅精确断言能区分。

正交性总结：A 跳过整段、C 丢值、B 转置键值、D 错置布尔分隔、E 错用分隔符——五种不同的故障表现，全部规避了仅检查 `sys._xoptions={}` 的 P2P 测试。

## 验证方法

环境：CPython 3.11（`/home/sankuai/conda/envs/ng311`，因 base 3.8 缺 `backports.zoneinfo`）。应用 golden patch + test_patch 后 commit 为 POST-PATCH 基线，`utils_tests.test_autoreload.TestChildArguments` 全 9 项通过。每个候选从干净 HEAD 应用 diff，`py_compile` 通过后运行该测试类。全部 5 个 mutation 均 `py_compile` 成功且令 `test_xoptions` 失败（failures=1），其余 8 项保持通过。
