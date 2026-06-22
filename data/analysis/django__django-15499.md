# django__django-15499

## 问题背景

迁移优化时，`CreateModel + AlterModelManagers` 应被合并为单个带 managers 的 `CreateModel`（类似 `CreateModel + AlterModelOptions` 已支持的优化）。Golden patch 在 `CreateModel.reduce` 中新增一个 `elif` 分支：当后续操作是 `AlterModelManagers` 且同名时，返回带 `operation.managers` 的新 `CreateModel`。

## Golden Patch 语义分析

```python
elif (
    isinstance(operation, AlterModelManagers)
    and self.name_lower == operation.name_lower
):
    return [
        CreateModel(
            self.name, fields=self.fields, options=self.options,
            bases=self.bases, managers=operation.managers,
        ),
    ]
```
核心语义：**识别 `CreateModel` 后紧跟同名 `AlterModelManagers`，把后者的 `managers` 合并进 `CreateModel` 并消除 Alter 操作**。关键三点：(1) `isinstance(operation, AlterModelManagers)` 正确判型；(2) `self.name_lower == operation.name_lower` 同名判定；(3) 用 `operation.managers`（新 managers）而非 `self.managers`（CreateModel 原本的空 managers）。

F2P 测试 `OptimizerTests.test_create_alter_model_managers`：`CreateModel("Foo") + AlterModelManagers("Foo", managers=[...])` 应优化为单个带这些 managers 的 `CreateModel`。

## 调用链分析

迁移优化器对相邻操作两两调用 `reduce`。`CreateModel.reduce(operation)` 按 operation 类型走不同 elif 分支。AlterModelManagers 分支若判型错、判名错、用错 managers 源、或返回 None，都会让优化失败（要么不合并、要么 managers 丢失）。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `managers=operation.managers`→`self.managers`，合并后用了空 managers |
| B | ➕ 补充 | 新增 | 原缺 B 组（isinstance 判错类型） |
| C | 🟢 高质量 | 保留 | `operation.name_lower`→`operation.name`，大小写不匹配致同名判定失败 |
| D | 🔴 必须替换 | 替换 | 原 D=A（同 `self.managers`）；改为 `return None` |
| E | 🟢 高质量 | 保留 | 把合并藏到 `merge_into_create` 开关后，默认 return None |

原 A=D 重复（都 `self.managers`）。补 B，重做 D。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：用错 managers 源）
```diff
-                    managers=operation.managers,
+                    managers=self.managers,
```
**变异语义**：合并时用 `self.managers`（CreateModel 原本的空 managers）而非 `operation.managers`（AlterModelManagers 带来的新 managers）。优化后的 CreateModel managers 为空，与期望的两个 manager 不符，F2P 失败。模拟"该取 operation 的值却取了 self 的"。保留。

### Group B — 补充（B2 判型错误）
```diff
-            isinstance(operation, AlterModelManagers)
+            isinstance(operation, AlterModelOptions)
             and self.name_lower == operation.name_lower
```
**变异语义**：把 `isinstance(operation, AlterModelManagers)` 判型改成 `AlterModelOptions`。由于 `AlterModelOptions` 已在上方分支处理，且实际 operation 是 `AlterModelManagers`，本分支永不匹配 → 不发生合并优化，F2P 失败。模拟"判型写成了相邻的另一个 Alter 类型"。

### Group C — 保留（A3/边界：判名属性错）
```diff
             isinstance(operation, AlterModelManagers)
-            and self.name_lower == operation.name_lower
+            and self.name_lower == operation.name
```
**变异语义**：同名判定用 `operation.name`（原始大小写）而非 `operation.name_lower`（小写规范化）。`self.name_lower` 是小写，与未规范化的 `operation.name`（如 "Foo"）不等，同名判定失败 → 不合并。模拟 `name`/`name_lower` 混淆。保留。

### Group D — 替换（D1 状态：返回 None）
**原**：与 A 重复（`self.managers`）。
**最终 mutation**：
```diff
         elif (
             isinstance(operation, AlterModelManagers)
             and self.name_lower == operation.name_lower
         ):
-            return [
-                CreateModel(
-                    self.name, fields=self.fields, options=self.options,
-                    bases=self.bases, managers=operation.managers,
-                ),
-            ]
+            return None
```
**变异语义**：分支匹配后直接 `return None`，表示"无法优化"。于是 CreateModel + AlterModelManagers 不被合并，仍是两个操作，F2P 期望的单 CreateModel 落空。模拟"分支占位、忘了实现合并逻辑"。

### Group E — 保留（E2 隐式→显式参数）
```diff
+            if not operation.merge_into_create:
+                return None
             return [ ... managers=operation.managers ... ]
```
并给 `AlterModelManagers.__init__` 加 `merge_into_create=False` 参数。
**变异语义**：把合并行为藏到 `merge_into_create` 开关后，默认 False → 默认 `return None` 不合并。模拟"把优化做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

原 A=D 重复（都把 `operation.managers` 换成 `self.managers`）。本次保留 A（错 managers 源）、C（错判名属性）、E（默认关闭开关），补充 B（判型改成 AlterModelOptions），把重复的 D 改为 `return None`。五组覆盖"managers 源 / 判型 / 判名 / 返回 None / 默认关闭开关"五个角度。全部实测：golden 通过、变异令 F2P 失败、`base→golden→test_patch` 后干净应用。
