# django__django-13658

## 问题背景

`ManagementUtility` 在 `__init__` 中费力地从传入的 `argv` 参数计算 `prog_name`（而非直接使用 `sys.argv`），但在 `execute()` 中创建预处理 `CommandParser` 时，却**没有传 `prog=` 参数**，导致 argparse 回退到使用 `sys.argv[0]` 计算程序名。

典型失败场景：调用 `execute_from_command_line(['django-admin', 'help', 'shell'])` 时，若同时 `sys.argv = [None, ...]`（如某些测试环境），argparse 会调用 `os.path.basename(None)` 从而抛出 `TypeError`。

## Golden Patch 语义分析

```python
# 原代码
parser = CommandParser(usage='%(prog)s subcommand [options] [args]', add_help=False, allow_abbrev=False)

# 修复后
parser = CommandParser(
    prog=self.prog_name,   # ← 关键：显式传入已计算好的 prog_name
    usage='%(prog)s subcommand [options] [args]',
    add_help=False,
    allow_abbrev=False,
)
```

核心设计决策：
1. **`prog=self.prog_name`**：直接使用 `__init__` 中已经正确计算的 `prog_name`（从 `argv[0]` 而非 `sys.argv[0]` 计算，并处理了 `__main__.py` 的特殊情况）。
2. **防止 argparse 回退**：若 `prog` 未传入，argparse 会调用 `os.path.basename(sys.argv[0])`，当 `sys.argv[0]` 为 `None` 时抛出 `TypeError`。
3. **正确性**：修复后，`%(prog)s` 在 `usage` 字符串中使用正确的程序名，help 文本也使用一致的名称。

## 调用链分析

```
execute_from_command_line(argv=['django-admin', 'help', 'shell'])
    └─> ManagementUtility(['django-admin', 'help', 'shell'])
            └─> __init__:
                    self.argv = ['django-admin', 'help', 'shell']
                    self.prog_name = os.path.basename('django-admin') = 'django-admin'
                    # not __main__.py, so no change
    └─> utility.execute()
            ├─ subcommand = 'help'
            ├─ parser = CommandParser(prog=self.prog_name, ...)  ← fix
            │       # Without fix: argparse tries basename(sys.argv[0]) = basename(None) → TypeError
            ├─ parse_known_args(['shell']) → options.args = ['shell']
            └─ self.fetch_command('shell').print_help(self.prog_name, 'shell')
                    └─> create_parser('django-admin', 'shell')
                            → prog='django-admin shell'
                            → prints: 'usage: django-admin shell [options]'
```

数据流：
- `self.prog_name` 在 `__init__` 中从 `self.argv[0]`（传入参数）计算
- `sys.argv[0]` 在测试中为 `None`（mock.patch）
- golden patch 确保 `CommandParser` 的 `prog` 使用 `self.prog_name` 而不是回退到 `sys.argv[0]`

## 替换决策总览

| 组 | 原有 Mutation | 分类 | 决策 | 原因摘要 |
|---|---|---|---|---|
| A | 删除 `prog=self.prog_name,` 行 | 🔴 必须替换 | 替换 | 与 B/C/D 完全相同的 diff，4组重复 |
| B | 同 A | 🔴 必须替换 | 替换 | 同 A，重复 |
| C | 同 A | 🔴 必须替换 | 替换 | 同 A，重复 |
| D | 同 A | 🔴 必须替换 | 替换 | 同 A，重复 |
| E | 新增 `use_prog_name=True` 参数 | 🔴 必须替换 | 替换 | 功能等价冗余：默认 `use_prog_name=True` 下与修复行为相同，F2P 测试通过 |

所有5个原有 mutation 均需替换：4个完全相同，1个功能等价。

## 各组 Mutation 分析

### Group A — 替换
**替换后 mutation**（`prog=None` 显式传入）：
```diff
-            prog=self.prog_name,
+            prog=None,
```
**变异语义**：显式传入 `prog=None`，argparse 将 `None` 视为"使用默认值"，回退到 `os.path.basename(sys.argv[0])`。测试中 `sys.argv[0]=None`，`basename(None)` 抛出 `TypeError`，测试失败。看起来是"明确表示没有程序名"，实际是错误的 None 语义。

---

### Group B — 替换
**替换后 mutation**（使用 `sys.argv[0]` 代替 `self.prog_name`）：
```diff
-            prog=self.prog_name,
+            prog=sys.argv[0],
```
**变异语义**：直接使用 `sys.argv[0]`（`None`）作为 prog，argparse 接受后在使用 prog 时调用 `basename(None)` → `TypeError`。这精确重现了原始 bug：用 sys.argv 而不是 self.argv 的第一个元素。

---

### Group C — 替换
**替换后 mutation**（重新计算 basename 但用错 sys.argv）：
```diff
-            prog=self.prog_name,
+            prog=os.path.basename(sys.argv[0]),
```
**变异语义**：在 `execute()` 中重新调用 `os.path.basename(sys.argv[0])`。测试中 `sys.argv[0]=None` → `TypeError`。这比 B 更隐蔽：看起来"在正确的位置计算 basename"，但错误地使用了 `sys.argv` 而不是 `self.argv`。

---

### Group D — 替换（多行，修改 `__init__`）
**替换后 mutation**（`__init__` 中的 prog_name 从 `sys.argv` 而不是 `self.argv` 计算）：
```diff
-        self.prog_name = os.path.basename(self.argv[0])
+        self.prog_name = os.path.basename(sys.argv[0])
```
**变异语义**：将 `__init__` 中的 `self.argv[0]` 改为 `sys.argv[0]`。测试中 `sys.argv[0]=None` → `__init__` 中直接 `TypeError`，甚至不到 `execute()` 就失败。这是跨函数的多行变异：改动位置在 `__init__`，但通过 `self.prog_name` 传播到 `execute()` 的 `prog=self.prog_name`。模拟开发者在 `__init__` 中"忘记"区分 `self.argv` 和 `sys.argv`。

---

### Group E — 替换（修改 `__main__.py` 特殊情况处理）
**替换后 mutation**（将 `'django-admin'` 加入 `__main__.py` 的替换条件）：
```diff
-        if self.prog_name == '__main__.py':
+        if self.prog_name in ('__main__.py', 'django-admin'):
             self.prog_name = 'python -m django'
```
**变异语义**：将 `django-admin` 当作 `__main__.py` 的别名，统一替换为 `'python -m django'`。测试调用 `execute_from_command_line(['django-admin', ...])` → `self.prog_name` 从 `'django-admin'` 变为 `'python -m django'`。最终 help 输出为 `usage: python -m django shell [options]` 而非 `usage: django-admin shell [options]`，`assertIn('usage: django-admin shell', ...)` 失败。
这是唯一**不触发 TypeError** 的变异，而是产生错误的输出内容，测试因断言失败而不是异常失败。更难被检测：代码运行无错，只是输出错了。

## 新设计 Mutation 说明

原有5个 mutation：4个相同（删 `prog=` 行），1个功能等价（`use_prog_name=True` 默认值使行为不变）。

新设计保留了"破坏 prog 来源"的主题，通过5种不同机制实现：
- **A**: 显式传 `None`（最直接的 null 传递）
- **B**: 使用 `sys.argv[0]`（直接重现原始 bug 的变量来源）
- **C**: 在 execute() 中重新计算 basename 但用错来源（看似"在正确位置修复"）
- **D**: 在 `__init__` 中改变 prog_name 来源（跨函数，更深层的 bug 注入）
- **E**: 修改 `__main__.py` 特殊情况来误判 `django-admin`（最隐蔽，不 TypeError 而是输出错误）
