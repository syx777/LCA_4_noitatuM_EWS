# django__django-13809

## 问题背景

`runserver` 命令缺少 `--skip-checks` 选项，无法跳过系统检查。该补丁：

1. 在 `add_arguments()` 中添加 `--skip-checks` 参数（`action='store_true'`，存储为 `skip_checks`）
2. 在 `inner_run()` 中将系统检查调用包裹在 `if not options['skip_checks']:` 条件中

**重要背景**：`runserver` 的 `requires_system_checks = []`（空列表），因此 `BaseCommand.execute()` 不会自动处理系统检查——检查是在 `inner_run()` 中手动调用的。其他命令通过 `BaseCommand.create_parser()` 自动添加 `--skip-checks` 参数，但 `runserver` 需要自己添加。

## Golden Patch 语义分析

**两处修改**：

```python
# 1. add_arguments(): 添加 --skip-checks 参数
parser.add_argument(
    '--skip-checks', action='store_true',  # 存储为 skip_checks（默认 False）
    help='Skip system checks.',
)

# 2. inner_run(): 条件包裹检查调用
if not options['skip_checks']:
    self.stdout.write('Performing system checks...\n\n')
    self.check(display_num_errors=True)
```

`action='store_true'`：默认为 `False`（正常运行检查），传入 `--skip-checks` 后设为 `True`（跳过检查）。

## 调用链分析

```
call_command('runserver', skip_checks=True)
  └─ Django.call_command():
       └─ opt_mapping: {'skip-checks' -> dest} = {'skip_checks': 'skip_checks'}
       └─ arg_options = {'skip_checks': True}
       └─ 合并 argparse 默认值与 arg_options
       └─ command.execute(**options)
            └─ BaseCommand.execute():
                 └─ requires_system_checks=[] -> 不自动检查
                 └─ handle() -> run() -> inner_run()
                      └─ if not options['skip_checks']: (True -> not True = False -> 跳过)
                           └─ 不执行 stdout.write 和 self.check()
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 语义浅层 | 替换 | 条件直接反转，质量可以更好 |
| C | 语义浅层 | 替换 | dest 名称错误导致 KeyError，但太直白 |
| E | 必须替换 | 替换 | 人工 `or not options.get('respect_skip_checks', False)` 旗标，不自然 |
| B | — | 新增 | 为 B 组新增 |
| D | — | 新增 | 为 D 组新增 |

注：原 A、C 均可保留优化版本；原 E 不自然，必须替换。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：`if options['skip_checks']:` (直接反转)

**分类**：🟡 语义浅层（但质量尚可，此处保留改进版）

**最终 mutation**（B3 — 反转布尔逻辑）：
```diff
-        if not options['skip_checks']:
+        if options['skip_checks']:
```
**变异语义**：将条件从"不跳过时运行检查"改为"跳过时运行检查"。F2P Part 1（`skip_checks=True`）：条件为 True → 运行检查 → `assert_not_called()` FAIL。F2P Part 2（`skip_checks=False`）：条件为 False → 不运行检查 → `assert_called()` FAIL。两部分均失败。模拟开发者对"skip_checks 为 True 时应该做什么"的逻辑误解。

---

### Group B — 新增

**最终 mutation**（B2 — 只跳过输出，不跳过检查调用）：
```diff
         if not options['skip_checks']:
             self.stdout.write('Performing system checks...\n\n')
-            self.check(display_num_errors=True)
+        self.check(display_num_errors=True)
```
**变异语义**：`stdout.write` 被条件保护，但 `self.check()` 移到 if 块外，始终执行。F2P Part 1（`skip_checks=True`）：无 stdout 输出，但 `check()` 仍被调用 → `mocked_check.assert_not_called()` FAIL。F2P Part 2 PASS（正常运行，check 被调用）。P2P（原有测试）PASS。模拟开发者认为"skip_checks 只跳过进度输出，不跳过实际检查执行"的理解错误，这是真实可能发生的误解：开发者可能认为检查必须运行，只是不需要显示输出。

---

### Group C — 替换

**原 mutation**：`dest='skip_system_checks'` 导致 KeyError（已有 diff）

**分类**：🟡 语义浅层（dest 名称一字之差导致 KeyError）

**最终 mutation**（A2 — 改变参数 dest 名称）：
```diff
-            '--skip-checks', action='store_true',
+            '--skip-checks', action='store_true', dest='skip_system_checks',
```
**变异语义**：`--skip-checks` 的 `dest` 改为 `skip_system_checks`，但 `inner_run()` 仍读取 `options['skip_checks']`。通过 Django 的 `call_command` 时，`opt_mapping['skip_checks'] = 'skip_system_checks'`，传入的 `skip_checks=True` kwarg 被映射到 `skip_system_checks=True`，而 `skip_checks` 键不存在于 options → `inner_run` 中 `options['skip_checks']` → `KeyError`。F2P FAIL。模拟开发者在添加参数时使用了与代码读取不一致的 dest 名称。

---

### Group D — 新增

**最终 mutation**（E2 — 错误的 action 类型，语义反转）：
```diff
-            '--skip-checks', action='store_true',
+            '--skip-checks', action='store_false', dest='skip_checks',
```
**变异语义**：`action='store_false'` 使得 `--skip-checks` 旗标被传入时将 `skip_checks` 设为 `False`（而非 `True`），且默认值为 `True`（跳过检查！）。通过 `call_command` 时：显式传入 `skip_checks=True/False` 绕过了 argparse 默认值，直接使用 kwarg 值，所以 F2P PASS。但 CLI 用法时：不传 `--skip-checks` → 默认 `True` → 始终跳过检查！传入 `--skip-checks` → 设为 `False` → 运行检查（语义完全反转）。模拟开发者混淆 `store_true` 和 `store_false` 的使用场景，这是常见错误（尤其是在修改旗标的语义时）。仅通过 `call_command` 测试（F2P/P2P）检测不到，需要 CLI 集成测试。

---

### Group E — 替换

**原 mutation**：`or not options.get('respect_skip_checks', False)` 人工旗标，不自然。

**最终 mutation**（D3 — 遗漏 add_arguments 中的参数声明）：
```diff
-        parser.add_argument(
-            '--skip-checks', action='store_true',
-            help='Skip system checks.',
-        )
```
**变异语义**：`--skip-checks` 参数未在 `add_arguments()` 中声明，但 `inner_run()` 仍包含 `if not options['skip_checks']:` 条件。通过 `call_command(skip_checks=True/False)` 时，kwarg 直接注入 options dict，绕过 argparse，所以 F2P PASS。但 CLI 用法时：运行 `python manage.py runserver --skip-checks` → argparse 报错 `unrecognized arguments: --skip-checks`。模拟开发者实现了 `inner_run` 中的逻辑但忘记在 `add_arguments` 中声明对应的 CLI 参数——实现了功能逻辑，但未暴露接口。这种错误在代码审查中难以发现，因为 `call_command` 测试全部通过。

---

## 新设计 Mutation 说明

### B 设计说明
`self.stdout.write` 和 `self.check()` 应该同时被 `skip_checks` 条件保护。将 `check()` 移出 if 块模拟了开发者认为"检查必须始终运行，但可以不显示进度信息"的设计决策错误。F2P 测试明确验证了 `mocked_check.assert_not_called()`，所以此 mutation 被精确地捕获。

### D 设计说明
`store_true` 和 `store_false` 的区别：
- `store_true`: 默认 False，传入 flag 后设为 True
- `store_false`: 默认 True，传入 flag 后设为 False

开发者可能在实现"跳过检查"时错误选择了 `store_false`，导致：未传 flag → 跳过（应运行）；传 flag → 运行（应跳过）。

### E 设计说明
开发者实现了功能（`inner_run` 中的条件判断）但未完成接口声明（`add_arguments` 中的参数添加）。这是一种典型的不完整实现，在测试套件只使用 `call_command` 时难以发现，需要完整的 CLI 集成测试才能暴露。
