# django__django-12708

## 问题背景

当一个 Django 模型同时定义了 `unique_together` 和 `index_together` 指向相同字段时，执行删除 `index_together` 的迁移会崩溃并抛出 `ValueError: Found wrong number (2) of constraints`。

根本原因在于 `alter_index_together` 调用 `_delete_composed_index` 时传递的 `constraint_kwargs` 为 `{'index': True}`，而在数据库中，`unique_together` 生成的约束在某些后端（如 PostgreSQL）也被标记为 `index=True`，导致 `_constraint_names` 同时找到了 index 约束和 unique 约束两个，触发数量校验错误。

Golden patch 的修复：将 kwargs 改为 `{'index': True, 'unique': False}`，明确排除 unique 约束，只查找纯粹的索引。

## Golden Patch 语义分析

修复只改动了 `alter_index_together` 函数中调用 `_delete_composed_index` 时传递的 `constraint_kwargs` 参数：

```python
# 修复前
self._delete_composed_index(model, fields, {'index': True}, self.sql_delete_index)

# 修复后
self._delete_composed_index(
    model,
    fields,
    {'index': True, 'unique': False},
    self.sql_delete_index,
)
```

核心逻辑：`_constraint_names` 方法根据 `constraint_kwargs` 过滤数据库约束。增加 `unique: False` 参数后，`_constraint_names` 中的过滤逻辑 `if unique is not None and infodict['unique'] != unique: continue` 会跳过所有 `unique=True` 的约束，从而只返回纯索引约束，避免同时找到 index 和 unique 两个约束。

## 调用链分析

```
alter_index_together(model, old_index_together, new_index_together)
  └── _delete_composed_index(model, fields, {'index': True, 'unique': False}, sql_delete_index)
        └── _constraint_names(model, columns, exclude=..., index=True, unique=False)
              └── connection.introspection.get_constraints(cursor, table)  [DB 查询]
        └── _delete_constraint_sql(sql, model, constraint_names[0])
              └── execute(...)  [执行 DROP]
```

同时，`alter_unique_together` 也调用 `_delete_composed_index`，传递 `{'unique': True}`：
```
alter_unique_together(model, old_unique_together, new_unique_together)
  └── _delete_composed_index(model, fields, {'unique': True}, sql_delete_unique)
        └── _constraint_names(model, columns, exclude=..., unique=True)
```

`_constraint_names` 是核心过滤函数，根据多个维度（unique/index/primary_key/foreign_key/check/type_/exclude）筛选数据库中的约束名称列表。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | 原 mutation 不存在（缺失） |
| B | 必须替换 | 替换 | 原 mutation 将 `!= 1` 改为 `== 1`，逻辑完全反转，不自然 |
| C | 必须替换 | 替换 | 原 mutation 将 set 改为 list，导致 `list \| set` TypeError，必然崩溃 |
| D | 必须替换 | 替换 | 原 mutation 不存在（缺失） |
| E | 必须替换 | 替换 | 原 mutation 直接还原 golden patch（逆操作），属于直接冗余 |

语义浅层共 0 个。全部为必须替换。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：（不存在，需新设计）

**分类**：🔴 必须替换（缺失）

**理由**：A 组不存在于 mutations.jsonl，需为 "API Specifications & Contracts" 策略设计一个高质量 mutation。

**最终 mutation**：
```diff
diff --git a/django/db/backends/base/schema.py b/django/db/backends/base/schema.py
index 2e2d7bbb60..9e21b56eae 100644
--- a/django/db/backends/base/schema.py
+++ b/django/db/backends/base/schema.py
@@ -1158,7 +1158,7 @@ class BaseDatabaseSchemaEditor:
         result = []
         for name, infodict in constraints.items():
             if column_names is None or column_names == infodict['columns']:
-                if unique is not None and infodict['unique'] != unique:
+                if unique is not None and infodict['unique'] == unique:
                     continue
                 if primary_key is not None and infodict['primary_key'] != primary_key:
                     continue
```

**变异语义**：将 `_constraint_names` 中的 unique 过滤逻辑从"跳过不匹配的"改为"跳过匹配的"——即过滤语义完全相反。当调用方传入 `unique=False` 时，原逻辑会跳过 `unique=True` 的约束（保留 unique=False 的），突变后则跳过 `unique=False` 的约束（保留 unique=True 的）。这导致删除 index_together 时找到的是 unique 约束而非 index 约束，并删除错误对象或抛出数量错误。对于大多数只测试 unique=None 或 unique=True 的场景，行为正常；只在 unique=False 时失败，即 index_together 删除场景。

---

### Group B — 替换

**原 mutation**：
```diff
@@ -412,7 +412,7 @@ class BaseDatabaseSchemaEditor:
             model, columns, exclude=meta_constraint_names | meta_index_names,
             **constraint_kwargs
         )
-        if len(constraint_names) != 1:
+        if len(constraint_names) == 1:
             raise ValueError("Found wrong number (%s) of constraints for %s(%s)" % (
```

**分类**：🔴 必须替换

**理由**：将 `!= 1` 改为 `== 1`，使校验逻辑完全反转——恰好找到1个约束时抛出错误，找到多个或0个时反而不报错。这种符号替换极不自然，代码审查时立即可见。

**最终 mutation**：
```diff
diff --git a/django/db/backends/base/schema.py b/django/db/backends/base/schema.py
index 2e2d7bbb60..c3389f7fbe 100644
--- a/django/db/backends/base/schema.py
+++ b/django/db/backends/base/schema.py
@@ -409,7 +409,7 @@ class BaseDatabaseSchemaEditor:
         meta_index_names = {constraint.name for constraint in model._meta.indexes}
         columns = [model._meta.get_field(field).column for field in fields]
         constraint_names = self._constraint_names(
-            model, columns, exclude=meta_constraint_names | meta_index_names,
+            model, columns, exclude=meta_constraint_names,
             **constraint_kwargs
         )
         if len(constraint_names) != 1:
```

**变异语义**：从 `exclude` 集合中移除 `meta_index_names`，意味着 `Meta.indexes` 中声明的显式索引名不再被排除出搜索范围。当模型同时定义了 `Meta.indexes` 和 `index_together`（指向相同字段），`_constraint_names` 将找到两个索引约束，触发 "Found wrong number (2)" 错误。仅在有 Meta.indexes 与 index_together/unique_together 同字段共存时失败，普通场景下表现正常。

---

### Group C — 替换

**原 mutation**：
```diff
@@ -405,7 +405,7 @@ class BaseDatabaseSchemaEditor:
             self.execute(self._create_index_sql(model, fields, suffix="_idx"))
 
     def _delete_composed_index(self, model, fields, constraint_kwargs, sql):
-        meta_constraint_names = {constraint.name for constraint in model._meta.constraints}
+        meta_constraint_names = [constraint.name for constraint in model._meta.constraints]
         meta_index_names = {constraint.name for constraint in model._meta.indexes}
```

**分类**：🔴 必须替换

**理由**：将 set comprehension 改为 list comprehension，在 `meta_constraint_names | meta_index_names` 时触发 `TypeError: unsupported operand type(s) for |: 'list' and 'set'`，导致任何调用 `_delete_composed_index` 的场景都崩溃，明显可检测。

**最终 mutation**：
```diff
diff --git a/django/db/backends/base/schema.py b/django/db/backends/base/schema.py
index 2e2d7bbb60..57366c0b0a 100644
--- a/django/db/backends/base/schema.py
+++ b/django/db/backends/base/schema.py
@@ -407,7 +407,7 @@ class BaseDatabaseSchemaEditor:
     def _delete_composed_index(self, model, fields, constraint_kwargs, sql):
         meta_constraint_names = {constraint.name for constraint in model._meta.constraints}
         meta_index_names = {constraint.name for constraint in model._meta.indexes}
-        columns = [model._meta.get_field(field).column for field in fields]
+        columns = tuple(model._meta.get_field(field).column for field in fields)
         constraint_names = self._constraint_names(
             model, columns, exclude=meta_constraint_names | meta_index_names,
             **constraint_kwargs
```

**变异语义**：将 `columns` 从 `list` 改为 `tuple`。在 `_constraint_names` 中，对比使用 `column_names == infodict['columns']`，而 `infodict['columns']` 是由数据库内省返回的 `list`。`tuple == list` 在 Python 中永远为 `False`，因此任何约束都无法匹配，`_constraint_names` 返回空列表，触发 "Found wrong number (0) of constraints" 错误。表现为调用 `_delete_composed_index` 必然失败，但不是因为语法错误，而是因为隐式类型不兼容——`list` 和 `tuple` 相等性比较返回 `False`。普通代码审查中 `[...]` 和 `tuple(...)` 看起来都是合理的序列构造。

---

### Group D — 替换

**原 mutation**：（不存在，需新设计）

**分类**：🔴 必须替换（缺失）

**理由**：D 组不存在于 mutations.jsonl，需为 "I/O & Environment Handling" 策略设计一个高质量 mutation。

**最终 mutation**：
```diff
diff --git a/django/db/backends/base/schema.py b/django/db/backends/base/schema.py
index 2e2d7bbb60..b44bb334a1 100644
--- a/django/db/backends/base/schema.py
+++ b/django/db/backends/base/schema.py
@@ -405,8 +405,8 @@ class BaseDatabaseSchemaEditor:
             self.execute(self._create_index_sql(model, fields, suffix="_idx"))
 
     def _delete_composed_index(self, model, fields, constraint_kwargs, sql):
-        meta_constraint_names = {constraint.name for constraint in model._meta.constraints}
-        meta_index_names = {constraint.name for constraint in model._meta.indexes}
+        meta_constraint_names = {constraint.name for constraint in model._meta.indexes}
+        meta_index_names = {constraint.name for constraint in model._meta.constraints}
         columns = [model._meta.get_field(field).column for field in fields]
         constraint_names = self._constraint_names(
             model, columns, exclude=meta_constraint_names | meta_index_names,
```

**变异语义**：交换 `meta_constraint_names` 和 `meta_index_names` 的数据来源——`meta_constraint_names` 现在包含的是 Meta.indexes 的名称，而 `meta_index_names` 包含的是 Meta.constraints 的名称。由于两者最终都参与 union 并传给 `exclude`，在模型没有 Meta.constraints/Meta.indexes 时效果相同（都是空集）；但在模型定义了 `Meta.indexes` 时，真正的 Meta.index 名称会被错误地放入 `meta_constraint_names` 而非 `meta_index_names`——虽然此处 union 结果一样，但若后续代码单独使用这两个变量，会产生混乱。更重要的是：当模型有 Meta.constraints 时，Meta.constraint 名称进入 meta_index_names（原应进入 meta_constraint_names），导致 Meta.constraint 约束不被 exclude，从而被 `_constraint_names` 找到，引发数量错误。此 mutation 模拟了开发者在初始化/配置对象状态时混淆两个相似集合变量的真实错误。

---

### Group E — 替换

**原 mutation**：
```diff
@@ -396,7 +396,7 @@ class BaseDatabaseSchemaEditor:
             self._delete_composed_index(
                 model,
                 fields,
-                {'index': True, 'unique': False},
+                {'index': True},
                 self.sql_delete_index,
             )
```

**分类**：🔴 必须替换

**理由**：直接还原 golden patch——将 `{'index': True, 'unique': False}` 改回 `{'index': True}`，等同于把修复逆操作。这是最典型的直接冗余类型。

**最终 mutation**：
```diff
diff --git a/django/db/backends/base/schema.py b/django/db/backends/base/schema.py
index 2e2d7bbb60..c94dbd6060 100644
--- a/django/db/backends/base/schema.py
+++ b/django/db/backends/base/schema.py
@@ -1158,7 +1158,7 @@ class BaseDatabaseSchemaEditor:
         result = []
         for name, infodict in constraints.items():
             if column_names is None or column_names == infodict['columns']:
-                if unique is not None and infodict['unique'] != unique:
+                if unique and infodict['unique'] != unique:
                     continue
                 if primary_key is not None and infodict['primary_key'] != primary_key:
                     continue
```

**变异语义**：将 `_constraint_names` 的 unique 过滤条件从 `unique is not None` 改为 `unique`（Python 真值检测）。`None` 和 `False` 都是 falsy，因此：
- 当 `unique=None`（不过滤）：两者均不激活过滤，行为相同。
- 当 `unique=True`：`True` 是 truthy，过滤仍然激活，行为相同。
- 当 `unique=False`：`False` 是 falsy，过滤不再激活！原本应该跳过 `unique=True` 的约束，现在全部保留。

结果：调用 `_delete_composed_index(model, fields, {'index': True, 'unique': False}, ...)` 时，`unique=False` 传入 `_constraint_names`，但过滤条件 `if unique and ...` 不生效，同时找到 index 约束和 unique 约束，触发 "Found wrong number (2)" 错误。此 mutation 将 `is not None` 与 `truthy check` 混淆，是开发者在 Python 空值检测中的典型失误，代码审查中极难发现。

## 新设计 Mutation 说明

### Group A（新设计）

基于对 `_constraint_names` 过滤逻辑的深层理解。该函数的核心是：对每个约束，逐一检查调用方传入的过滤条件，若约束属性与条件不符则跳过。mutation 将 unique 维度的"不等则跳过"改为"相等则跳过"，精确地反转了 unique 过滤语义，而不影响 index/primary_key/foreign_key 等其他维度的过滤。这模拟了开发者在写条件逻辑时将"过滤掉不符合条件的"误写成"过滤掉符合条件的"的真实错误，类似于 `filter` 和 `reject` 混淆。

### Group D（新设计）

基于对 `_delete_composed_index` 中两个语义相近变量的分析。`meta_constraint_names`（来自 `model._meta.constraints`，即显式 Meta.constraints）和 `meta_index_names`（来自 `model._meta.indexes`，即显式 Meta.indexes）的名称非常相似，且在此处都用于构建排除集合。mutation 交换两者的数据来源，模拟了开发者在填充两个相似变量时不小心写反的真实错误。由于两者都参与 union，在大多数场景下效果相同（因为一般模型的这两个集合都是空的），只在模型同时使用 Meta.constraints 或 Meta.indexes 时才暴露错误。
