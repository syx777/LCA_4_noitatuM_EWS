# django__django-16595

## 问题背景

迁移优化器不会归并连续的多个 `AlterField`。当一系列 `AlterField`（对同一字段）与 `AddField` 分隔开时（如非 elidable 迁移），优化器不会把它们折叠成最后一个。根因：`AlterField.reduce` 只处理 `operation` 是 `RemoveField` 的情况，没考虑 `operation` 也是 `AlterField`。Golden patch 把 `isinstance(operation, RemoveField)` 扩展为 `isinstance(operation, (AlterField, RemoveField))`，使连续 AlterField 也能折叠成后者。

## Golden Patch 语义分析

```python
def reduce(self, operation, app_label):
    if isinstance(
        operation, (AlterField, RemoveField)
    ) and self.is_same_field_operation(operation):
        return [operation]
    elif ...
```
核心语义：**当后续 operation 是针对同一字段的 `AlterField` 或 `RemoveField` 时，`reduce` 应返回 `[operation]`——即用后者取代前者（折叠）**。`is_same_field_operation(operation)` 确认是同一模型同一字段。返回 `[operation]`（后一个操作）丢弃 self（前一个 AlterField），因为对同字段的连续 alter 只有最后一个生效。把 `AlterField` 加入 isinstance 元组是修复关键——原来只有 RemoveField 能触发折叠。

F2P 测试 `OptimizerTests.test_alter_alter_field`：两个对同字段的 `AlterField` 应折叠成第二个（经 `_test_alter_alter` 断言 optimize 后只剩后者）。

## 调用链分析

迁移优化器对相邻操作两两调 `reduce`。`AlterField.reduce(operation, app_label)`：若 operation 是同字段的 AlterField/RemoveField，返回 `[operation]`（折叠为后者）；否则检查 RenameField 等其它分支。`is_same_field_operation` 比较 model_name/name。返回 `[operation]` 表示"self 与 operation 合并为 operation"。isinstance 元组缺 AlterField、返回值错（self 而非 operation）、is_same 判断反转，都会让连续 AlterField 不折叠或错误折叠。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 补充 | 新增 | `return [operation]`→`return [self]`，折叠成前一个而非后一个 |
| B | 🟢 高质量 | 保留 | isinstance 元组去掉 AlterField，只剩 RemoveField（还原 bug） |
| C | 🔴 必须替换 | 替换 | 原 C 去掉整个 isinstance；改为 `(AddField, RemoveField)` 错误类型 |
| D | 🔴 必须替换 | 替换 | 原 D 与 B 等价（`(RemoveField,)`）；改为 `not is_same_field_operation` |
| E | 🟢 高质量 | 保留 | AlterField 折叠藏到默认关闭的 `reduce_alter_field` 开关后 |

原 B、C、D 都围绕 isinstance 元组（B 去 AlterField、C 去整个 isinstance、D 用单元素元组），机制趋同。保留 B、E，补充 A、重做 C、D。

## 各组 Mutation 分析

### Group A — 补充（A1 接口契约：返回前一个操作）
```diff
         ) and self.is_same_field_operation(operation):
-            return [operation]
+            return [self]
```
**变异语义**：折叠时返回 `[self]`（前一个 AlterField）而非 `[operation]`（后一个）。对同字段连续 alter，应保留**最后**一个的字段定义，返回 self 会保留**第一个**——折叠方向错误。`test_alter_alter_field`（第二个 AlterField 带 `help_text="help"`，期望折叠成它）失败，因为折叠成了第一个。模拟"折叠保留了错误的那一端"。隐蔽——确实折叠了、只是留错了对象。

### Group B — 保留（A1 接口契约：元组缺 AlterField）
```diff
-        if isinstance(
-            operation, (AlterField, RemoveField)
-        ) and self.is_same_field_operation(operation):
+        if isinstance(operation, RemoveField) and self.is_same_field_operation(operation):
```
**变异语义**：isinstance 元组去掉 `AlterField`，只匹配 `RemoveField`。连续 AlterField 不再走折叠分支（落到 elif 或不归并），还原原 bug——多个 AlterField 不被 reduce。`test_alter_alter_field` 失败。保留。

### Group C — 替换（C1 类型：错误的操作类型）
**原**：去掉整个 `isinstance(...)`，只留 `self.is_same_field_operation(operation)`。
**最终 mutation**：
```diff
-            operation, (AlterField, RemoveField)
+            operation, (AddField, RemoveField)
```
**变异语义**：isinstance 元组里把 `AlterField` 换成 `AddField`。AlterField-AlterField 不再匹配（AlterField 不是 AddField/RemoveField 实例）→ 连续 AlterField 不折叠；同时 AddField 通常不会作为 `reduce` 的 operation 传来匹配同字段，故该项形同虚设。`test_alter_alter_field` 失败。模拟"列举操作类型时写错了一个类名（Add vs Alter）"。比 B（直接删 AlterField）隐蔽——元组里还有两个类型、看着像正常组合。

### Group D — 替换（B3 条件反转：is_same 取反）
**原**：与 B 等价（`(RemoveField,)` 单元素元组）。
**最终 mutation**：
```diff
-        ) and self.is_same_field_operation(operation):
+        ) and not self.is_same_field_operation(operation):
             return [operation]
```
**变异语义**：把 `is_same_field_operation` 取反。只有 operation 与 self **不是**同一字段时才折叠——而折叠同字段操作才是本意。同字段的连续 AlterField（`is_same` 为真）→ `not` 为假 → 不折叠；不同字段的操作反而被错误折叠（丢数据）。语义颠倒。`test_alter_alter_field` 失败。模拟"is_same 守卫写反"。

### Group E — 保留（E2 隐式→显式开关）
```diff
-    def __init__(self, model_name, name, field, preserve_default=True):
+    def __init__(self, model_name, name, field, preserve_default=True, reduce_alter_field=False):
         self.preserve_default = preserve_default
+        self.reduce_alter_field = reduce_alter_field
...
         ) and self.is_same_field_operation(operation):
+            if isinstance(operation, AlterField) and not self.reduce_alter_field:
+                return super().reduce(operation, app_label)
             return [operation]
```
**变异语义**：新增 `reduce_alter_field` 参数（默认 False），AlterField-AlterField 折叠只在该开关开启时生效，否则走 `super().reduce`（基类不折叠）。RemoveField 折叠仍正常。默认构造的 AlterField 不传该参数 → 连续 AlterField 不折叠。模拟"把 AlterField 折叠做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

原 B、C、D 都围绕 `reduce` 的 isinstance 元组（B 去 AlterField、C 去整个 isinstance、D 用 `(RemoveField,)`），机制高度趋同。本次保留 B（元组去 AlterField）、E（reduce_alter_field 默认关闭开关），补充 A（`return [self]` 折叠方向错），重做 C（`AddField` 错误类型替换 AlterField）、D（`not is_same_field_operation` 守卫反转）。五组覆盖"返回值方向 / 元组缺 AlterField / 错误类型 / is_same 反转 / 默认关闭开关"五个角度，分别作用于返回值、isinstance 类型、字段判断、开关四个不同环节。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
