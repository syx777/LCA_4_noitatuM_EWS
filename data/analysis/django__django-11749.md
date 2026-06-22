# django__django-11749 Mutation 策展分析

## 问题背景

`call_command` 在通过 `**kwargs` 传入「必填互斥组（required mutually exclusive group）」的参数时会失败。例如：

```python
shop = parser.add_mutually_exclusive_group(required=True)
shop.add_argument('--shop-id', nargs='?', type=int, default=None, dest='shop_id')
shop.add_argument('--shop', nargs='?', type=str, default=None, dest='shop_name')
```

调用 `call_command('my_command', shop_id=1)` 会抛出
`CommandError: Error: one of the arguments --shop-id --shop is required`，
而命令行 `call_command('my_command', '--shop-id=1')` 正常。

根因：`call_command` 只把「单个 action 自身 `required=True`」的 kwargs 转发给 `parse_args()`。但互斥组成员单独看 `required` 都是 `False`，是「组」要求 `required=True`，因此组成员永远不会被转发，导致 argparse 认为必填组缺参。

## Golden Patch 语义分析

文件 `django/core/management/__init__.py` 的 `call_command`：

1. 新增集合 `mutually_exclusive_required_options`，遍历 `parser._mutually_exclusive_groups`，对 `group.required` 为真的组收集其全部 `_group_actions`。
2. 把转发条件由 `opt.required and opt.dest in options` 改为
   `opt.dest in options and (opt.required or opt in mutually_exclusive_required_options)`。

即：只要该 option 在 kwargs 中，且它「自身必填」或「属于某个必填互斥组」，就把它拼成 `--name=value` 加入 `parse_args`，从而让 argparse 满足必填组约束。

## 调用链分析

- 入口：`management.call_command(command_name, *args, **options)`。
- `command.create_parser('', command_name)` 构造 argparse `parser`，`add_arguments` 中通过 `add_mutually_exclusive_group(required=True)` 注册互斥组。
- `parser._mutually_exclusive_groups` / `group._group_actions` / `group.required` 为 argparse 内部结构，是补丁的数据来源。
- 收集到的 option 通过 `'{}={}'.format(min(opt.option_strings), arg_options[opt.dest])` 拼成命令行串，追加进 `parse_args`，再交给 `parser.parse_args(args=parse_args)`。
- F2P 测试 `tests/user_commands/tests.py::CommandTests::test_mutually_exclusive_group_required_options`，配套命令 `tests/user_commands/management/commands/mutually_exclusive_required.py`（必填互斥组，成员 `--foo-id` / `--foo-name`，均 `default=None`）。断言：`call_command(..., foo_id=1)` 与 `foo_name='foo'` 成功，且不传任何参数时抛出指定 `CommandError`。

## 替换决策总览

输入 3 个 mutation（组 A / B / D）。

| 组 | 原 diff 摘要 | 分类 | 决策 | 最终策略码 |
|----|--------------|------|------|------------|
| A | `if group.required` → `if not group.required` | 🟡 浅层，但「外科手术式」仅 F2P 失败 | 保留 | A1 |
| B | `or` → `and`（合并集合判断） | 🟡 浅层，且破坏 4 个既有 P2P 测试，极易被检出 | 替换 | B3（新设计） |
| D | 整个集合推导 → `set()  # Bug: not properly initialized` | 🔴 含 "Bug" 注释的人工痕迹 | 替换 | D1（新设计） |

浅层 mutation 数 M=2（A、B），需替换最弱的 floor(2/2)=1 个浅层 + 全部 🔴。
关键判断：B 的 `or→and` 会破坏所有正常必填参数转发（实测使 `test_call_command_with_required_parameters_in_options` 等 4 个既有 P2P 测试报错），属「最弱浅层」（易被现有测试检出），故替换 B 而非 A。A 反而是外科手术式（仅 F2P 失败），符合「关键控制流节点、真实边界失误」的保留标准。

## 各组 Mutation 分析

### 组 A —— 保留（A1）

- 原 diff：

```diff
-        for opt in group._group_actions if group.required
+        for opt in group._group_actions if not group.required
```

- 分类：🟡 SEMANTIC-SHALLOW 单 token（`group.required` → `not group.required`）。
- 理由：对必填组取反后集合为空，kwargs 互斥组参数不再转发，精确复现原始 bug；且对普通必填参数路径无影响，实测 `user_commands` 模块仅 F2P 失败、其余全过。属于真实「条件取反」失误，保留。
- 最终 diff：同上。
- 变异语义：必填判断被反转，使「必填互斥组」被当作「非必填」处理，组成员永不转发。

### 组 B —— 替换（原 → B3）

- 原 diff：

```diff
-            (opt.required or opt in mutually_exclusive_required_options)
+            (opt.required and opt in mutually_exclusive_required_options)
```

- 分类：🟡 浅层单 token（`or`→`and`），但为「最弱」：`and` 使所有「自身必填」option 也必须同时属于互斥组才转发，直接破坏既有 4 个 P2P 测试（`test_call_command_with_required_parameters_in_options` 等），会被现有测试集轻易检出。
- 决策：替换。
- 新最终 diff（B3，Invert/alter boolean logic，新增条件项）：

```diff
-            (opt.required or opt in mutually_exclusive_required_options)
+            (opt.required or (opt in mutually_exclusive_required_options and opt.default is not None))
```

- 变异语义：在互斥组成员的判定上追加 `opt.default is not None` 合取项。互斥组的典型写法 `default=None`（F2P 命令正是如此）会使该项为 `False`，option 被丢弃，重现必填组报错；而 default 非 None 的场景仍正常。属于条件组合型 bug，只在「default=None 的必填互斥组」组合下暴露。

### 组 D —— 替换（原 → D1）

- 原 diff：

```diff
-    mutually_exclusive_required_options = {
-        opt
-        for group in parser._mutually_exclusive_groups
-        for opt in group._group_actions if group.required
-    }
+    mutually_exclusive_required_options = set()  # Bug: not properly initialized
```

- 分类：🔴 MUST REPLACE。含字面 "Bug: not properly initialized" 注释，为明显人工痕迹，不自然。
- 决策：替换。
- 新最终 diff（D1，Break state/collection initialization，off-by-one 切片）：

```diff
-        for opt in group._group_actions if group.required
+        for opt in group._group_actions[:1] if group.required
```

- 变异语义：集合初始化时用 `[:1]` 只收集每个必填互斥组的「第一个」action。传第一个成员（`foo_id`）仍可工作，但传后续成员（`foo_name`）不被转发，触发必填组报错。是真实的「不完整集合初始化 / off-by-one」失误，仅在非首位互斥成员被传入时失败，与 A（整组置空）失败模式正交。

## 新设计 Mutation 说明

三个最终 mutation 失败模式正交、覆盖不同子策略：

- A1：必填判断取反 → 整个必填组被忽略（所有成员都失败）。
- B3：对互斥组成员追加 `default is not None` 合取 → 仅 `default=None` 的互斥成员失败（条件组合）。
- D1：集合切片 `[:1]` → 仅互斥组的非首位成员失败（不完整初始化 / off-by-one）。

三者均：仅修改 `django/core/management/__init__.py`（不动测试）、非 golden 平凡回退、`py_compile` 通过、能通过典型/普通必填参数测试。

## 实测验证结果

环境：`PYTHONPATH=<tmp>:$PYTHONPATH`，从临时仓库运行 `tests/runtests.py`。

- 基线（golden + test_patch，无 mutation）：F2P `test_mutually_exclusive_group_required_options` **PASS（OK）**。
- A1：`py_compile` OK；F2P **FAILED**；`user_commands` 全模块（`--parallel 1`）仅该 F2P 失败（1 error），其余 P2P 全通过。
- B3：`py_compile` OK；F2P **FAILED**；全模块仅该 F2P 失败（1 error），P2P 全通过。
- D1：`py_compile` OK；F2P **FAILED**；全模块仅该 F2P 失败（1 error），P2P 全通过。

失败信息均为：
`CommandError: Error: one of the arguments --foo-id --foo-name is required`，与原始 bug 表现一致。

对照（被替换项）：原 B（`or→and`）实测破坏 `test_call_command_with_required_parameters_in_options`、`test_call_command_with_required_parameters_in_mixed_options`、`test_subparser_dest_required_args` 等 4 个测试，故判为最弱浅层并替换。
