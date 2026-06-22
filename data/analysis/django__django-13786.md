# django__django-13786

## 问题背景

`squashmigrations` 优化器在将 `CreateModel` + `AlterModelOptions` 合并时，没有清除 `CreateModel` 已有但 `AlterModelOptions` 未保留的选项。原来的代码仅使用 `{**self.options, **operation.options}` 合并，保留了所有旧选项，而 `AlterModelOptions.state_forwards()` 中正确的逻辑会遍历 `ALTER_OPTION_KEYS` 并删除不在新 options 中的键。

例如：
```python
CreateModel('MyModel', fields=[], options={'verbose_name': 'My Model'})
AlterModelOptions('MyModel', options={})
```
优化后应产生 `CreateModel('MyModel', fields=[])` (无选项)，但原代码产生 `CreateModel('MyModel', fields=[], options={'verbose_name': 'My Model'})`（保留了旧 verbose_name）。

## Golden Patch 语义分析

**修复核心**：在 `CreateModel.reduce()` 的 `AlterModelOptions` 分支中，引入与 `AlterModelOptions.state_forwards()` 相同的清除逻辑：

```python
options = {**self.options, **operation.options}  # 1. 先合并
for key in operation.ALTER_OPTION_KEYS:           # 2. 遍历所有受控 option 键
    if key not in operation.options:              # 3. 如果新 op 未设置该键
        options.pop(key, None)                    # 4. 从结果中移除（清除旧值）
```

关键：`ALTER_OPTION_KEYS` 只包含 `AlterModelOptions` 可以控制的选项（verbose_name、ordering 等），不包含 `proxy`、`constraints` 等 schema 级别的选项，所以只有 `ALTER_OPTION_KEYS` 中的键会被清除，其他选项不受影响。

## 调用链分析

```
MigrationOptimizer.optimize_inner()
  └─ CreateModel.reduce(AlterModelOptions, app_label)
       ├─ isinstance(op, AlterModelOptions) and name_lower matches
       ├─ options = {**self.options, **op.options}   # merge
       ├─ for key in op.ALTER_OPTION_KEYS:           # clear stale keys
       │    if key not in op.options: options.pop(key)
       └─ return [CreateModel(name, fields, options, bases, managers)]

AlterModelOptions.state_forwards(app_label, state):
  └─ model_state.options = {**model_state.options, **self.options}
  └─ for key in self.ALTER_OPTION_KEYS:
       if key not in self.options: model_state.options.pop(key, False)  # same logic
```

两处逻辑的语义完全一致：合并后清除 ALTER_OPTION_KEYS 中未被新 op 显式设置的键。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | `{**op.options}` 忽略 self.options 且条件反转，直接是 golden 的逆操作 |
| B | 必须替换 | 替换 | 条件反转导致删除所有在新 op 中设置的键，P2P 测试失败 |
| C | 必须替换 | 替换 | 删除整个 for 循环，等同于还原原始 bug |
| D | — | 新增 | 为 D 组新增高质量 mutation |
| E | 必须替换 | 替换 | `clear_options_on_alter=False` 旗标使修复代码永远不执行，不自然 |

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**（必须替换）：
```diff
-            options = {**self.options, **operation.options}
+            options = {**operation.options}
             for key in operation.ALTER_OPTION_KEYS:
-                if key not in operation.options:
+                if key in operation.options:
                     options.pop(key, None)
```
**理由**：双重错误（仅用 op.options + 条件反转），P2P 新增选项测试也失败，属于质量极差 mutation。

**最终 mutation**（A1 — 合并顺序错误）：
```diff
diff --git a/django/db/migrations/operations/models.py b/django/db/migrations/operations/models.py
index 7e8becb100..4082c1b3fb 100644
--- a/django/db/migrations/operations/models.py
+++ b/django/db/migrations/operations/models.py
@@ -137,7 +137,7 @@ class CreateModel(ModelOperation):
                 ),
             ]
         elif isinstance(operation, AlterModelOptions) and self.name_lower == operation.name_lower:
-            options = {**self.options, **operation.options}
+            options = {**operation.options, **self.options}
             for key in operation.ALTER_OPTION_KEYS:
                 if key not in operation.options:
                     options.pop(key, None)
```
**变异语义**：将合并顺序从 `{**self.options, **op.options}`（op 覆盖 self）改为 `{**op.options, **self.options}`（self 覆盖 op）。这使得 `AlterModelOptions` 中更新的值不能覆盖旧值。F2P-1（op={}）通过，F2P-2（op 设新 verbose_name）失败：结果保留旧值 `'My Model'` 而非新值 `'My New Model'`。P2P（self={}）通过（空 self 不影响）。模拟开发者弄反字典合并优先级的真实失误。

---

### Group B — 替换

**原 mutation**（必须替换）：
```diff
-                if key not in operation.options:
+                if key in operation.options:
```
**理由**：条件反转，P2P（在 AlterModelOptions 中新增选项）也会失败，将新设置的选项反而删除。

**最终 mutation**（B3 — 布尔逻辑错误）：
```diff
diff --git a/django/db/migrations/operations/models.py b/django/db/migrations/operations/models.py
index 7e8becb100..f189e6d47b 100644
--- a/django/db/migrations/operations/models.py
+++ b/django/db/migrations/operations/models.py
@@ -139,7 +139,7 @@ class CreateModel(ModelOperation):
         elif isinstance(operation, AlterModelOptions) and self.name_lower == operation.name_lower:
             options = {**self.options, **operation.options}
             for key in operation.ALTER_OPTION_KEYS:
-                if key not in operation.options:
+                if key not in operation.options or key in self.options:
                     options.pop(key, None)
```
**变异语义**：将 `not in op` 改为 `not in op OR in self`，即当某个 key 曾存在于 self.options 时也删除（即使 op 重新设置了它）。F2P-1（op={}）通过（所有键都 not in op）。F2P-2 失败：`verbose_name` 在 op 中重新设置，但 `key in self.options` 为真，导致被删除，结果为 `{}`。P2P（self={}）通过（self 中没有任何键）。模拟开发者认为"旧 self 中有的 key 必须总被清除"的错误直觉。

---

### Group C — 替换

**原 mutation**（必须替换）：
```diff
-            for key in operation.ALTER_OPTION_KEYS:
-                if key not in operation.options:
-                    options.pop(key, None)
```
**理由**：直接删除 for 循环，等同于还原原始 bug（不清除旧选项），属于直接冗余。

**最终 mutation**（C1 — 错误的迭代对象）：
```diff
diff --git a/django/db/migrations/operations/models.py b/django/db/migrations/operations/models.py
index 7e8becb100..8e13891e94 100644
--- a/django/db/migrations/operations/models.py
+++ b/django/db/migrations/operations/models.py
@@ -138,7 +138,7 @@ class CreateModel(ModelOperation):
             ]
         elif isinstance(operation, AlterModelOptions) and self.name_lower == operation.name_lower:
             options = {**self.options, **operation.options}
-            for key in operation.ALTER_OPTION_KEYS:
+            for key in list(options.keys()):
                 if key not in operation.options:
                     options.pop(key, None)
```
**变异语义**：将 `operation.ALTER_OPTION_KEYS` 改为 `list(options.keys())`，即不再限制于 ALTER_OPTION_KEYS 控制的键，而是遍历合并后所有选项。对 F2P 测试场景行为相同（所有测试 options 都在 ALTER_OPTION_KEYS 中），但对包含 `proxy`、`constraints` 等 schema 级别 option 的 CreateModel 会产生错误清除。P2P 通过，F2P-1/F2P-2 通过。模拟开发者认为"应该清除所有不在新 op 中的选项"而非只清除 ALTER 控制的选项。

---

### Group D — 新增

**原 mutation**：（无，新设计）

**最终 mutation**（D1 — 条件门控清除逻辑）：
```diff
diff --git a/django/db/migrations/operations/models.py b/django/db/migrations/operations/models.py
index 7e8becb100..84494a3218 100644
--- a/django/db/migrations/operations/models.py
+++ b/django/db/migrations/operations/models.py
@@ -138,8 +138,8 @@ class CreateModel(ModelOperation):
             ]
         elif isinstance(operation, AlterModelOptions) and self.name_lower == operation.name_lower:
             options = {**self.options, **operation.options}
-            for key in operation.ALTER_OPTION_KEYS:
-                if key not in operation.options:
+            if not operation.options:
+                for key in operation.ALTER_OPTION_KEYS:
                     options.pop(key, None)
```
**变异语义**：将清除逻辑用 `if not operation.options:` 门控，只在 op 为空时清除旧选项。F2P-1（op={}）通过（触发清除）。F2P-2 失败：op 非空（有 verbose_name 更新）→ 不触发清除 → 结果为 `{'verbose_name': 'My New Model', 'verbose_name_plural': 'My Models'}`（多出 verbose_name_plural）。P2P 通过（op 非空，不清除，self={} 本无旧选项）。模拟开发者认为"只有 op 完全清空时才需要清除旧选项"的边界判断失误——忽略了"部分更新也需要清除不再控制的键"的情况。

---

### Group E — 替换

**原 mutation**（必须替换）：`clear_options_on_alter=False` 旗标控制，永远不执行清除代码，且增加了不自然的参数。

**最终 mutation**（E2 — 条件门控反转）：
```diff
diff --git a/django/db/migrations/operations/models.py b/django/db/migrations/operations/models.py
index 7e8becb100..3ac9ccee08 100644
--- a/django/db/migrations/operations/models.py
+++ b/django/db/migrations/operations/models.py
@@ -138,9 +138,10 @@ class CreateModel(ModelOperation):
             ]
         elif isinstance(operation, AlterModelOptions) and self.name_lower == operation.name_lower:
             options = {**self.options, **operation.options}
-            for key in operation.ALTER_OPTION_KEYS:
-                if key not in operation.options:
-                    options.pop(key, None)
+            if operation.options:
+                for key in operation.ALTER_OPTION_KEYS:
+                    if key not in operation.options:
+                        options.pop(key, None)
```
**变异语义**：将清除逻辑用 `if operation.options:` 门控，只在 op 有选项时清除旧值（与 D 的 `if not operation.options:` 相反）。F2P-1（op={}）失败：op 为空 → 不触发清除 → 结果保留 `{'verbose_name': 'My Model'}` 而非 `{}`。F2P-2 通过（op 非空，触发清除）。P2P 通过（op 非空）。模拟开发者认为"只有当 op 真的有选项时才需要清理操作"，反向地忽略了"空 op 正是要清除所有旧选项的情况"。

---

## 新设计 Mutation 说明

### A 设计说明
字典合并时，`{**a, **b}` 中 b 的值覆盖 a 的值。原 golden 的顺序 `{**self.options, **op.options}` 保证 op 的新值优先（正确）。反转为 `{**op.options, **self.options}` 使 self（旧值）优先覆盖 op（新值），导致 AlterModelOptions 中的更新无法生效。这是开发者在处理 "merge with override" 语义时最常见的顺序错误。

### B 设计说明  
`or key in self.options` 的添加使得条件从"该 key 未被新 op 设置"变为"该 key 未被新 op 设置 OR 曾存在于旧 self 中"，对于同时满足第二个条件的 key 会过度删除，导致 op 明确设置的新值也被删除。

### C 设计说明
`list(options.keys())` 遍历合并后的所有 option 键，不再受 ALTER_OPTION_KEYS 白名单限制，会错误清除 `proxy=True` 等 schema 级别 option。但由于 F2P 测试只用了 ALTER_OPTION_KEYS 中的键，这个差异在测试中不可见——只在特殊场景下（含非 ALTER 类 option 的 CreateModel）才会暴露。

### D 设计说明
`if not operation.options:` 将清除行为限制在"完全重置 options"的场景（op 为空），忽略了"部分更新时仍需清除不再设置的旧键"。这是一个合理的误解：开发者可能认为"有选项就是设置，没选项才是清空"，但 AlterModelOptions 的语义是"设置的选项有效，未列出的选项应被清除"。

### E 设计说明
`if operation.options:` 与 D 的条件互补（D: not op, E: op）。E 的失败场景恰好是 F2P-1（op={}），而 D 的失败场景是 F2P-2（op 非空但有未更新的旧键）。两者从不同方向展示了对"何时需要清除旧选项"的误判。
