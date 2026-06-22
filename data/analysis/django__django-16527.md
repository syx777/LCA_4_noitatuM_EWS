# django__django-16527

## 问题背景

Django admin 的 "Save as new"（另存为新对象）按钮的显示条件缺少新增权限检查。`show_save_as_new` 仅校验了 `has_change_permission`、`change`、`save_as`，没校验 `has_add_permission`——而"另存为新"本质是一次新增操作。结果只有修改权限、没有新增权限的用户也能看到并使用该按钮，绕过权限。Golden patch 在 `show_save_as_new` 条件里加上 `and has_add_permission`。

## Golden Patch 语义分析

```python
"show_save_as_new": not is_popup
and has_add_permission       # ← 新增
and change
and save_as,
```
核心语义：**"另存为新"按钮的显示必须额外要求新增权限 `has_add_permission`**，因为该操作会创建新对象。原条件 `not is_popup and has_change_permission and change and save_as`（golden 之前还有 has_change_permission，patch 把那行替换为 has_add_permission）只验证了修改权限。修复后，只有同时具备 add 权限的用户才看到按钮。这是一个安全相关的权限校验补强——条件中每一个 `and` 项都是必要的过滤。

F2P 测试 `AdminTemplateTagsTest.test_submit_row_save_as_new_add_permission_required`：仅有 change 权限的用户断言 `show_save_as_new` 为 False；同时有 add+change 权限的用户断言为 True。

## 调用链分析

admin change 视图渲染时调 `submit_row(context)` 模板标签，它从 context 取 `has_add_permission`/`has_change_permission`/`is_popup`/`change`/`save_as` 等，组装出各按钮的显示布尔值字典，含 `show_save_as_new`。该值传给模板控制按钮渲染。`has_add_permission` 由 ModelAdmin 据用户权限算出。条件里若漏掉 `has_add_permission`、或用错权限、或逻辑运算符错，都会让无 add 权限的用户错误地看到按钮（F2P 第一个断言失败）或有权限者看不到（第二个断言失败）。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 删除 `and has_add_permission`，还原缺权限检查的 bug |
| B | 🟢 高质量 | 保留 | `and has_add_permission`→`or has_add_permission`，运算符优先级破坏整条逻辑 |
| C | 🔴 必须替换 | 替换 | 原 C 与 A/E 相同；改为 `has_change_permission`（用回错误的权限） |
| D | 🔴 必须替换 | 替换 | 原 D 与 A 相同（空行替换）；改为 `and True`（恒真占位，等于无检查） |
| E | 🔴 必须替换 | 替换 | 原 E 与 A 相同；改为默认关闭开关 gate |

原 A、D、E 字节相同（删除/空行替换 `and has_add_permission`）。保留 A、B，重做 C、D、E 为不同机制。

## 各组 Mutation 分析

### Group A — 保留（B2 删除条件项）
```diff
             "show_save_as_new": not is_popup
-            and has_add_permission
             and change
             and save_as,
```
**变异语义**：删除 `and has_add_permission`，条件退回只验证 change/save_as——还原原始权限漏洞。仅有 change 权限的用户 `show_save_as_new` 为 True（F2P 第一个断言期望 False，失败）。保留。

### Group B — 保留（B3 逻辑运算符：and→or）
```diff
             "show_save_as_new": not is_popup
-            and has_add_permission
+            or has_add_permission
```
**变异语义**：`and has_add_permission` 改成 `or has_add_permission`。由于 `and` 优先级高于 `or`，表达式变成 `(not is_popup) or (has_add_permission and change and save_as)`——只要不是 popup（常态）整个 show_save_as_new 就为 True，权限/change/save_as 全被短路忽略。无任何权限也显示按钮。模拟"and/or 写错破坏整条布尔链"。保留。

### Group C — 替换（A1 接口契约：用错权限）
**原**：与 A/E 相同（删除该行）。
**最终 mutation**：
```diff
             "show_save_as_new": not is_popup
-            and has_add_permission
+            and has_change_permission
```
**变异语义**：把新增的 `has_add_permission` 错写成 `has_change_permission`。这正是 golden 修复前的错误权限——"另存为新"是新增操作却校验修改权限。仅有 change 权限的用户仍能看到按钮（F2P 第一个断言失败）。模拟"修复时用错了权限属性名"。比删除该行隐蔽——条件项还在、只是权限选错。

### Group D — 替换（C1 值：恒真占位）
**原**：与 A 相同（空行替换该行）。
**最终 mutation**：
```diff
             "show_save_as_new": not is_popup
-            and has_add_permission
+            and True
```
**变异语义**：把 `has_add_permission` 替换成常量 `True`。条件结构完整（仍有 `and ...`），但该项恒真，等价于没有权限检查。无 add 权限的用户 show_save_as_new 仍为 True（F2P 第一个断言失败）。模拟"用 True 占位调试、忘了换回真实权限"。保留代码结构、只是该项失效。

### Group E — 替换（E2 隐式→显式开关）
**原**：与 A 相同（删除该行）。
**最终 mutation**：
```diff
             "show_save_as_new": not is_popup
-            and has_add_permission
+            and (has_add_permission or not context.get("enforce_add_perm_for_save_as", False))
```
**变异语义**：把权限检查包成 `has_add_permission or not context.get("enforce_add_perm_for_save_as", False)`，开关默认缺失（取 False）→ `not False == True` → 整个括号恒真 → 权限检查被旁路。只有 context 显式设 `enforce_add_perm_for_save_as=True` 时才真正校验 add 权限。模拟"把权限检查做成可配置、默认却关掉"。

## 新设计 Mutation 说明

原 A、D、E 字节相同（删除或空行替换 `and has_add_permission`）。本次保留 A（删除条件项）、B（and→or 破坏布尔链），重做 C（用回 `has_change_permission` 错误权限）、D（`and True` 恒真占位）、E（`enforce_add_perm_for_save_as` 默认关闭开关旁路检查）。五组覆盖"删除条件 / 运算符破坏 / 错误权限 / 恒真占位 / 默认关闭开关"五个角度——其中 A/C/D/E 都让无 add 权限者看到按钮（第一个断言失败），B 更彻底地短路全部条件。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
