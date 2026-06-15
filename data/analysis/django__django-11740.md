# django__django-11740

## 问题背景

用户将一个 `UUIDField` 字段改为 `ForeignKey`（`UUID → FK` 类型变更），Django 的 migration 自动检测器（`MigrationAutodetector`）生成的迁移文件**缺少对目标 app 的依赖声明**。例如 `otherapp.Book.author` 从 `IntegerField` 改为 `ForeignKey('testapp.Author', ...)`，生成的迁移应当在 `dependencies` 中包含 `('testapp', '__first__')`，但实际生成的迁移 `dependencies = []`，导致在运行迁移时出现 `ValueError: Related model 'testapp.Author' cannot be resolved`。

## Golden Patch 语义分析

Golden patch 在 `generate_altered_fields()` 方法中做了三处修改：

1. **初始化** `dependencies = []`：在获取 `old_field`/`new_field` 之后立即初始化，确保后续收集的依赖信息在 `add_operation` 调用时可用。

2. **收集 FK 依赖**：在处理完 `new_field.remote_field.model` 相关的字段重命名逻辑之后（`if hasattr(new_field, "remote_field") and getattr(new_field.remote_field, "model", None):` 块的末尾），调用：
   ```python
   dependencies.extend(self._get_dependencies_for_foreign_key(new_field))
   ```
   此调用向 `dependencies` 列表中添加形如 `(dep_app_label, dep_object_name, None, True)` 的 4-tuple，表示"在执行此 AlterField 前，目标模型所在 app 必须已被迁移"。

3. **传递依赖**：在 `add_operation(app_label, AlterField(...), dependencies=dependencies)` 调用中新增 `dependencies=dependencies` 参数，使 `operation._auto_deps` 携带所收集的依赖信息，进而被 migration graph 的 `arrange_for_graph` 阶段解析为跨 app 的 migration 依赖。

核心语义：**当字段类型从非关系型变为 FK 时，新字段的目标模型所在 app 的迁移必须先于当前迁移执行**，这与新建 FK 字段（`AddField`）时的依赖声明机制是一致的。

## 调用链分析

```
MigrationAutodetector.detect_changes()
  → generate_altered_fields()      ← 本次修复的位置
      → _get_dependencies_for_foreign_key(new_field)
          读取 new_field.remote_field.model._meta.app_label / object_name
          返回: [(dep_app_label, dep_object_name, None, True)]
      → add_operation(app_label, AlterField(...), dependencies=dependencies)
          设置 operation._auto_deps = dependencies
  → arrange_for_graph()
      遍历 _auto_deps，解析为 migration 的 .dependencies 列表
```

数据流：`new_field.remote_field.model` → `_get_dependencies_for_foreign_key` → `dependencies` 列表 → `operation._auto_deps` → 最终 migration 的 `.dependencies`。

## 替换决策总览

（注：mutations.jsonl 中仅存在 B/D/E 三组，缺少 A/C 两组，因此全部5组均需新设计）

| 组 | 原有类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 不存在 | 新设计 | 缺失，直接设计高质量 mutation |
| B | 🔴 必须替换 | 替换 | 等价于 `if old_field.remote_field:` 守卫，对 UUID→FK 场景功能等同于还原 bug |
| C | 不存在 | 新设计 | 缺失，直接设计高质量 mutation |
| D | 🔴 必须替换 | 替换 | `hasattr(old_field, "remote_field") and old_field.remote_field.model` 等价于 B，功能冗余 |
| E | 🔴 必须替换 | 替换 | `getattr(self, "_add_fk_dependencies", False)` 使用不存在的属性，明显人工痕迹 |

全部5组均替换为高质量 mutation。

## 各组 Mutation 分析

### Group A — 替换（新设计）

**原 mutation**：不存在

**分类**：新设计（A 组缺失）

**设计思路**：将 `dependencies.extend(...)` 调用的缩进增加4个空格，使其从 `if new_field.remote_field.model:` 块的末尾移入 `if from_fields:` 子块内部。

**最终 mutation**：
```diff
diff --git a/django/db/migrations/autodetector.py b/django/db/migrations/autodetector.py
index 1c40161c7f..4821b098c3 100644
--- a/django/db/migrations/autodetector.py
+++ b/django/db/migrations/autodetector.py
@@ -940,7 +940,7 @@ class MigrationAutodetector:
                         self.renamed_fields.get(rename_key + (to_field,), to_field)
                         for to_field in new_field.to_fields
                     ])
-                dependencies.extend(self._get_dependencies_for_foreign_key(new_field))
+                    dependencies.extend(self._get_dependencies_for_foreign_key(new_field))
             if hasattr(new_field, "remote_field") and getattr(new_field.remote_field, "through", None):
                 rename_key = (
                     new_field.remote_field.through._meta.app_label,
```

**变异语义**：`dependencies.extend(...)` 被移入 `if from_fields:` 块（用于处理 `ForeignObjects` 等多对多关系字段的 from_fields/to_fields 重命名）。对于普通的 `ForeignKey`（没有 `from_fields`），`from_fields = getattr(new_field, 'from_fields', None)` 返回 `None`，`if from_fields:` 块被跳过，因此 FK 依赖永远不会被收集。这个 mutation 看起来像是开发者误将 `extend` 调用写入了处理多字段 FK 的子块内，而非在所有 FK 类型统一收集依赖。仅测试 `ForeignObjects` 的用例能通过，标准 `ForeignKey` 变更的测试（如 `test_alter_field_to_fk_dependency_other_app`）会失败。

---

### Group B — 替换

**原 mutation**：
```diff
-                dependencies.extend(self._get_dependencies_for_foreign_key(new_field))
+                if old_field.remote_field:
+                    dependencies.extend(self._get_dependencies_for_foreign_key(new_field))
```

**分类**：🔴 必须替换

**理由**：`old_field.remote_field` 对非关系型字段（如 UUID）为 `None`，条件为 `False`，导致 UUID→FK 场景下依赖永不添加，与还原 golden patch 的效果等价。

**最终 mutation**：
```diff
diff --git a/django/db/migrations/autodetector.py b/django/db/migrations/autodetector.py
index 1c40161c7f..0f4911aa74 100644
--- a/django/db/migrations/autodetector.py
+++ b/django/db/migrations/autodetector.py
@@ -940,7 +940,7 @@ class MigrationAutodetector:
                         self.renamed_fields.get(rename_key + (to_field,), to_field)
                         for to_field in new_field.to_fields
                     ])
-                dependencies.extend(self._get_dependencies_for_foreign_key(new_field))
+                dependencies.append(self._get_dependencies_for_foreign_key(new_field))
             if hasattr(new_field, "remote_field") and getattr(new_field.remote_field, "through", None):
                 rename_key = (
                     new_field.remote_field.through._meta.app_label,
```

**变异语义**：将 `extend`（将可迭代对象的元素逐一追加）替换为 `append`（将整个可迭代对象作为单一元素追加）。`_get_dependencies_for_foreign_key` 返回 `[(app, model, None, True)]`（一个包含 4-tuple 的列表），`extend` 后 `dependencies = [(app, model, None, True)]`；`append` 后 `dependencies = [[(app, model, None, True)]]`（嵌套列表）。当 migration graph 的 `arrange_for_graph` 尝试将 `_auto_deps` 中的每个元素解包为 `(app_label, model_name, field_name, created)` 时，遇到的是一个 list 而非 tuple，导致解包错误（`ValueError: too many values to unpack`）。这是经典的 Python `extend` vs `append` 混淆错误，从代码审查角度极难发现（两个方法名仅差一个字母）。

---

### Group C — 替换（新设计）

**原 mutation**：不存在

**分类**：新设计（C 组缺失）

**设计思路**：在正确收集依赖后，用列表推导式过滤掉跨 app 的依赖，只保留与当前 `app_label` 相同的依赖项。

**最终 mutation**：
```diff
diff --git a/django/db/migrations/autodetector.py b/django/db/migrations/autodetector.py
index 1c40161c7f..1b8cc69533 100644
--- a/django/db/migrations/autodetector.py
+++ b/django/db/migrations/autodetector.py
@@ -941,6 +941,7 @@ class MigrationAutodetector:
                         for to_field in new_field.to_fields
                     ])
                 dependencies.extend(self._get_dependencies_for_foreign_key(new_field))
+                dependencies = [d for d in dependencies if d[0] == app_label]
             if hasattr(new_field, "remote_field") and getattr(new_field.remote_field, "through", None):
                 rename_key = (
                     new_field.remote_field.through._meta.app_label,
```

**变异语义**：FK 依赖被正确收集，但随后通过列表推导过滤掉所有 `dep_app_label != app_label` 的条目，只保留同 app 依赖。对于跨 app 的 FK（`otherapp.Book` 引用 `testapp.Author`），`dep_app_label = 'testapp'`，而 `app_label = 'otherapp'`，过滤后 `dependencies = []`，跨 app 依赖丢失，`test_alter_field_to_fk_dependency_other_app` 失败。对于同 app 内的 FK 变更，依赖可正确保留。这模拟了开发者"AlterField 只需声明本 app 内的依赖"的错误假设，看起来像一种防御性代码，但语义完全错误。

---

### Group D — 替换

**原 mutation**：
```diff
-                dependencies.extend(self._get_dependencies_for_foreign_key(new_field))
+                if hasattr(old_field, "remote_field") and getattr(old_field.remote_field, "model", None):
+                    dependencies.extend(self._get_dependencies_for_foreign_key(new_field))
```

**分类**：🔴 必须替换

**理由**：检查 `old_field.remote_field.model` 是否存在，对 UUID 字段 `old_field.remote_field` 为 `None`，`getattr(None, "model", None)` 为 `None`，条件为 `False`，功能等价于 B，同为对 golden fix 的直接还原。

**最终 mutation**：
```diff
diff --git a/django/db/migrations/autodetector.py b/django/db/migrations/autodetector.py
index 1c40161c7f..f2f3c98029 100644
--- a/django/db/migrations/autodetector.py
+++ b/django/db/migrations/autodetector.py
@@ -973,7 +973,7 @@ class MigrationAutodetector:
                             field=field,
                             preserve_default=preserve_default,
                         ),
-                        dependencies=dependencies,
+                        dependencies=dependencies if both_m2m else [],
                     )
                 else:
                     # We cannot alter between m2m and concrete fields
```

**变异语义**：修改 `add_operation` 调用中传递 `dependencies` 的条件：只有 `both_m2m`（新旧字段均为 M2M）时才传递收集到的依赖；其他情况（包括 `neither_m2m`，即非关系型→FK 这种情形）一律传 `[]`。UUID→FK 属于 `neither_m2m` 场景，`both_m2m = False`，因此 `dependencies=[]`，依赖丢失，测试失败。此 mutation 针对不同的代码位置（`add_operation` 的调用处而非 `extend` 处），模拟了开发者"只有 M2M 类型变更才需要 FK 依赖"的错误推断，与 A/B/C 的修改点完全不同，增加了 mutation 集的多样性。

---

### Group E — 替换

**原 mutation**：
```diff
-                dependencies.extend(self._get_dependencies_for_foreign_key(new_field))
+                if getattr(self, "_add_fk_dependencies", False):
+                    dependencies.extend(self._get_dependencies_for_foreign_key(new_field))
```

**分类**：🔴 必须替换（不自然：使用不存在于 `MigrationAutodetector` 的虚构属性 `_add_fk_dependencies`）

**理由**：明显人工痕迹，任何代码审查者都会立即发现该属性在整个类中未被定义，永远为 `False`。

**最终 mutation**：
```diff
diff --git a/django/db/migrations/autodetector.py b/django/db/migrations/autodetector.py
index 1c40161c7f..7e75cae273 100644
--- a/django/db/migrations/autodetector.py
+++ b/django/db/migrations/autodetector.py
@@ -940,7 +940,6 @@ class MigrationAutodetector:
                         self.renamed_fields.get(rename_key + (to_field,), to_field)
                         for to_field in new_field.to_fields
                     ])
-                dependencies.extend(self._get_dependencies_for_foreign_key(new_field))
             if hasattr(new_field, "remote_field") and getattr(new_field.remote_field, "through", None):
                 rename_key = (
                     new_field.remote_field.through._meta.app_label,
@@ -948,6 +947,7 @@ class MigrationAutodetector:
                 )
                 if rename_key in self.renamed_models:
                     new_field.remote_field.through = old_field.remote_field.through
+                dependencies.extend(self._get_dependencies_for_foreign_key(new_field))
             old_field_dec = self.deep_deconstruct(old_field)
             new_field_dec = self.deep_deconstruct(new_field)
             if old_field_dec != new_field_dec:
```

**变异语义**：将 `dependencies.extend(...)` 从 `if new_field.remote_field.model:` 块末尾**移入** `if new_field.remote_field.through:` 块末尾（M2M through-table 处理块）。对于普通 `ForeignKey`（无 through table），`getattr(new_field.remote_field, "through", None)` 为 `None`，整个 through 块被跳过，依赖永不收集，`test_alter_field_to_fk_dependency_other_app` 失败。对于带自定义 through 表的 M2M 字段变更，through 块会执行，FK 依赖可被收集（但此时调用 `_get_dependencies_for_foreign_key` 实际上会同时收集 through 模型的依赖，行为略有偏差）。这是一个双 hunk 的跨代码块迁移 mutation，看起来像是开发者"整理了代码结构"，实际上把 FK dep 收集逻辑放错了条件分支。

---

## 新设计 Mutation 说明

### Group A（缩进移入 from_fields 块）

基于对 `generate_altered_fields` 完整结构的分析，`if new_field.remote_field.model:` 块内部有三个子块：
1. rename_key 处理（model 重命名）
2. `if remote_field_name:` 处理（ForeignKey to_field 重命名）
3. `if from_fields:` 处理（ForeignObjects 的 from_fields/to_fields 重命名）

Golden fix 将 `dependencies.extend(...)` 放在第3个子块**之后**、与子块同级（16 spaces），确保对所有 FK 类型均生效。将其缩进4空格放入子块**之内**（20 spaces），使其只在 `ForeignObjects` 时执行，模拟了开发者"只有复杂 FK 对象才需要额外依赖"的错误理解。

### Group B（extend → append）

`list.extend(iterable)` 与 `list.append(item)` 的混淆是 Python 初学者和有经验开发者都容易犯的错误。`_get_dependencies_for_foreign_key` 返回列表，`extend` 将其元素展开追加（正确），`append` 将整个列表作为单一元素追加（嵌套列表，错误）。这一错误仅在 migration graph 尝试解包依赖元组时才暴露，极难通过静态分析发现。

### Group C（过滤跨 app 依赖）

通过分析 `_get_dependencies_for_foreign_key` 的返回格式 `(dep_app_label, dep_object_name, None, True)`，设计了基于 `d[0] == app_label` 的过滤条件。测试案例的跨 app 场景（`otherapp` → `testapp`）会被精确过滤掉，而同 app 场景能正常工作，体现了"同 app 依赖"vs"跨 app 依赖"的语义差异。

### Group D（M2M 条件守卫 add_operation 调用）

定位到 golden fix 的第三处修改（`dependencies=dependencies` 参数传递），利用 `both_m2m` 布尔变量（已在附近代码中定义）设计条件表达式，使非 M2M 的字段变更（`neither_m2m` 包括 UUID→FK）不传依赖。选择此位置是因为与 A/B/C/E 修改点不同，增加了代码覆盖多样性。

### Group E（移位到 through 块）

识别到文件中存在两个并列的 `if hasattr(new_field, "remote_field") and getattr(new_field.remote_field, "...", None):` 块（一个检查 `model`，一个检查 `through`），将 `extend` 调用从前者（正确）移至后者（错误），形成双 hunk 的跨块迁移 mutation。这种"看似整理了代码"的多行变化比单行符号替换更难在代码审查中发现。
