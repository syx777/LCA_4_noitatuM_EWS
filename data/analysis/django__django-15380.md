# django__django-15380

## 问题背景

在同一步里同时重命名模型和字段时，迁移自动检测器崩溃。根因在 `generate_renamed_fields`：取 `new_model_state` 时用了 `old_model_name` 作为键去查 `to_state`（新状态），而新状态里该模型已是新名字，导致 `KeyError`。Golden patch 把 `self.to_state.models[app_label, old_model_name]` 改成 `self.to_state.models[app_label, model_name]`（新状态用新名）。

## Golden Patch 语义分析

```python
old_model_name = self.renamed_models.get((app_label, model_name), model_name)
old_model_state = self.from_state.models[app_label, old_model_name]   # 旧状态用旧名
new_model_state = self.to_state.models[app_label, model_name]         # 新状态用新名（修复点）
field = new_model_state.get_field(field_name)
```
核心语义：**from_state（旧）按旧模型名查，to_state（新）按新模型名查**。两个状态的键必须各用对应名字。修复前 `new_model_state` 误用 `old_model_name` 查 `to_state`，而 to_state 中模型已重命名，故 `KeyError`。`new_model_state.get_field(field_name)` 取的是新字段名。

F2P 测试 `AutodetectorTests.test_rename_field_with_renamed_model`：把 Author→RenamedAuthor 且 name→renamed_name 同时重命名，断言生成 `['RenameModel','RenameField']` 两个操作而不崩溃。

## 调用链分析

`generate_renamed_fields` 遍历 `new_field_keys - old_field_keys`，对每个 (app, model, field) 算 `old_model_name`（经 `renamed_models` 映射），分别从 from_state/to_state 取旧/新模型状态，再扫描 `old_field_keys - new_field_keys` 找匹配做字段重命名判定。`from_state` 用旧名、`to_state` 用新名是关键约束；内层 `if rem_model_name == model_name` 用新模型名匹配。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 内层 `rem_model_name == model_name`→`== old_model_name`，重命名场景匹配失败 |
| B | 🔴 必须替换 | 替换 | 原 A=B 重复（同改内层）；改为还原 golden 行（new 用 old 名→KeyError） |
| C | 🟡 | 替换 | 原 C=D 重复（还原 golden 行）；改为 old_model_state 误从 to_state 取 |
| D | 🔴 必须替换 | 替换 | 与 C 重复；改为 old/new 都取自 to_state |
| E | ➕ 补充 | 新增 | 原缺 E 组 |

原 A=B（改内层匹配）、C=D（还原 golden 行）两两重复。重做 B/C/D 使五组机制各异，补充 E。

## 各组 Mutation 分析

### Group A — 保留（B3 条件语义：内层匹配用旧名）
```diff
-                if rem_app_label == app_label and rem_model_name == model_name:
+                if rem_app_label == app_label and rem_model_name == old_model_name:
```
**变异语义**：内层扫描旧字段时，把模型名匹配从新名 `model_name` 换成旧名 `old_model_name`。旧字段键里的 `rem_model_name` 是新状态视角的模型名（=新名），与 `old_model_name`（旧名）不等，匹配失败 → 找不到对应旧字段 → 重命名判定失败、生成错误操作序列。保留。

### Group B — 替换（D1 状态：new_model_state 用旧名→KeyError）
**原**：与 A 重复。
**最终 mutation**：
```diff
-            new_model_state = self.to_state.models[app_label, model_name]
+            new_model_state = self.to_state.models[app_label, old_model_name]
```
（即还原 golden 修复点）
**变异语义**：`new_model_state` 用 `old_model_name` 查 `to_state`。模型已重命名，to_state 无旧名键 → `KeyError`，autodetector 崩溃。这是 golden 修复的直接逆操作，作为 B 的代表（原 C/D 即此，去重后归并到 B）。

### Group C — 替换（A1 接口契约：old_model_state 误从 to_state 取）
**原**：与 D 重复。
**最终 mutation**：
```diff
-            old_model_state = self.from_state.models[app_label, old_model_name]
+            old_model_state = self.to_state.models[app_label, old_model_name]
```
**变异语义**：`old_model_state` 本应从 `from_state`（旧状态）按旧名取，却误从 `to_state`（新状态）取。新状态里该模型已是新名，旧名键不存在 → `KeyError`。模拟"from/to 状态搞反"——比直接改修复行更隐蔽，因为改的是相邻的 old_model_state 行。

### Group D — 替换（A1 接口契约：old/new 同源）
**原**：与 C 重复。
**最终 mutation**：
```diff
-            old_model_state = self.from_state.models[app_label, old_model_name]
-            new_model_state = self.to_state.models[app_label, model_name]
+            old_model_state = self.to_state.models[app_label, model_name]
+            new_model_state = self.to_state.models[app_label, model_name]
```
**变异语义**：把 `old_model_state` 也指向 `to_state[model_name]`，使新旧状态变成同一个对象。字段重命名判定依赖"旧状态有旧字段、新状态有新字段"的差异，两者同源后差异消失，重命名无法被检测，生成错误操作序列。模拟"复制粘贴上一行、忘改 state 来源"。

### Group E — 补充（E1 测试期望：field 取自旧状态）
```diff
-            field = new_model_state.get_field(field_name)
+            field = old_model_state.get_field(field_name)
```
**变异语义**：`field` 本应从 `new_model_state` 取新字段名 `field_name`，却从 `old_model_state` 取。旧状态里没有重命名后的字段名 → `KeyError`/取错字段，重命名判定失败。模拟"该用新状态取字段、却用了旧状态变量"。

## 新设计 Mutation 说明

原始 A=B（改内层匹配）、C=D（还原 golden 行）两两重复。本次保留 A（内层用旧名），把重复的 B 归并为"还原 golden 修复行（KeyError）"，C 改为"old_model_state 误从 to_state 取"，D 改为"old/new 同源 to_state"，并补充缺失的 E（field 取自旧状态）。五组覆盖"内层匹配名 / 修复行还原 / old 状态来源错 / old-new 同源 / field 来源错"五个角度，多个 KeyError 触发点各不相同。全部实测：golden 通过、变异令 F2P 失败、`base→golden→test_patch` 后干净应用。
