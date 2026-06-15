# django__django-11265

## 问题背景

在对带有 `FilteredRelation` 注解的 QuerySet 使用 `exclude()` 时，Django 抛出 `FieldError`，提示无法解析注解名（如 `book_alice`）。根本原因是 `exclude()` 在内部调用 `split_exclude()` 来生成子查询，但新建的子查询对象 `Query(self.model)` 初始化时 `_filtered_relations` 为空字典，无法解析父查询中已注册的 `FilteredRelation` 别名。

同时，即使名称解析问题被修复，`trim_start()` 方法也会错误地将带有 `filtered_relation` 的 INNER JOIN 裁剪掉，导致过滤条件丢失，查询结果错误（返回了不该返回的行）。

## Golden Patch 语义分析

Golden patch 修复了两个独立问题：

**修复1（`split_exclude` 第1668行）**：在创建内层子查询后、调用 `add_filter` 之前，将父查询的 `_filtered_relations` 传给内层查询：
```python
query._filtered_relations = self._filtered_relations
```
这使内层查询能够通过 `names_to_path()` 正确解析 `book_alice` 等 FilteredRelation 别名。

**修复2（`trim_start` 第2145行）**：在裁剪前置 JOIN 时，增加了对 `filtered_relation` 的检查：
```python
first_join = self.alias_map[lookup_tables[trimmed_paths + 1]]
if first_join.join_type != LOUTER and not first_join.filtered_relation:
```
原代码只检查 LOUTER，INNER JOIN 的 FilteredRelation 会被裁剪（`unref_alias`），从而丢失 FilteredRelation 中的 `ON` 子句过滤条件，导致子查询缺少约束。

## 调用链分析

```
QuerySet.exclude(book_alice__isnull=False)
  └─> Query.add_q(~Q(book_alice__isnull=False))
        └─> Query.build_filter(..., can_negate=True)
              └─> Query.split_exclude(filter_expr, can_reuse, names_with_path)
                    ├─> [新建] query = Query(self.model)  ← _filtered_relations = {}（bug点1）
                    ├─> query._filtered_relations = self._filtered_relations  ← fix1
                    ├─> query.add_filter(filter_expr)
                    │     └─> query.names_to_path(['book_alice', 'isnull'], ...)
                    │           └─> 查 self._filtered_relations['book_alice']  ← 若为空则 FieldError
                    └─> query.trim_start(names_with_path)
                          └─> 检查 first_join.join_type != LOUTER  ← fix2: 也要检查 filtered_relation
```

数据流：
- `_filtered_relations`：`{alias: FilteredRelation}` 映射，由 `add_filtered_relation()` 填充，在 `names_to_path()` 中用于将 `book_alice` 解析为真实 relation。
- `alias_map`：`{alias: Join}` 映射，`Join` 对象含 `join_type`（INNER/LOUTER）和 `filtered_relation` 属性。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | 原始 diff 直接还原 fix1（`_filtered_relations = {}`），是 golden patch 的简单逆操作 |
| B | 语义浅层 | 保留 | 翻转 `isinstance(filter_rhs, F)` 判断，位于逻辑入口，能模拟真实的类型判断误解 |
| C | 必须替换 | 替换 | 与 A 完全相同，重复 diff，必须替换为不同位置的 mutation |
| D | 新增（缺失） | 新增 | 仅还原 fix2（trim_start 检查），修改位置与 A/B/C 完全不同 |
| E | 新增（缺失） | 新增 | 在 clone() 中共享 _filtered_relations 引用，引入跨查询状态污染 |

语义浅层共 1 个（B），替换其中最弱的 floor(1/2)=0 个：无需替换 B。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index 08d7faf194..fbf0f1e08b 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1666,7 +1666,7 @@ class Query(BaseExpression):
             filter_expr = (filter_lhs, OuterRef(filter_rhs.name))
         # Generate the inner query.
         query = Query(self.model)
-        query._filtered_relations = self._filtered_relations
+        query._filtered_relations = {}
         query.add_filter(filter_expr)
         query.clear_ordering(True)
```

**分类**：🔴 必须替换

**理由**：这是 golden patch 修复1的直接逆操作，等同于将代码还原到 base_commit 状态，没有任何语义创新。

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index 08d7faf194..e0c4e12086 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1666,8 +1666,8 @@ class Query(BaseExpression):
             filter_expr = (filter_lhs, OuterRef(filter_rhs.name))
         # Generate the inner query.
         query = Query(self.model)
-        query._filtered_relations = self._filtered_relations
         query.add_filter(filter_expr)
+        query._filtered_relations = self._filtered_relations
         query.clear_ordering(True)
         # Try to have as simple as possible subquery -> trim leading joins from
         # the subquery.
```

**变异语义**：将 `_filtered_relations` 的赋值移到 `add_filter()` 之后。`add_filter` 内部调用 `names_to_path` 时需要解析 `book_alice`，但此时 `_filtered_relations` 仍为空，导致 `FieldError: Cannot resolve keyword 'book_alice'`。表面上看这只是语句顺序调整，代码审查很容易忽略，但实际上改变了内层查询的初始化时序。所有带 FilteredRelation 的 exclude 调用都会失败，包括 `test_with_exclude`。

---

### Group B — 保留

**原 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index 08d7faf194..19e2219595 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1662,7 +1662,7 @@ class Query(BaseExpression):
         handle.
         """
         filter_lhs, filter_rhs = filter_expr
-        if isinstance(filter_rhs, F):
+        if not isinstance(filter_rhs, F):
             filter_expr = (filter_lhs, OuterRef(filter_rhs.name))
```

**分类**：🟡 语义浅层（保留）

**理由**：位于 `split_exclude` 入口处的类型判断，翻转后使非 `F` 实例也尝试调用 `.name` 并包裹为 `OuterRef`，而 `F` 实例则不被包裹，直接进入子查询导致外部引用丢失。修改位置是关键控制流入口，能模拟开发者对 `F` 表达式判断逻辑的误解。

**最终 mutation**（保留原始）：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index 08d7faf194..19e2219595 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1662,7 +1662,7 @@ class Query(BaseExpression):
         handle.
         """
         filter_lhs, filter_rhs = filter_expr
-        if isinstance(filter_rhs, F):
+        if not isinstance(filter_rhs, F):
             filter_expr = (filter_lhs, OuterRef(filter_rhs.name))
```

**变异语义**：当 `filter_rhs` 不是 `F` 实例时（例如直接值、字符串等），代码会错误地调用 `filter_rhs.name` 并用 `OuterRef` 包裹，导致 `AttributeError` 或生成错误的外部引用。`F` 表达式则不被包裹，外层查询列不会作为外部引用传递，导致子查询逻辑错误。

---

### Group C — 替换

**原 mutation**（与 A 完全相同）：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index 08d7faf194..fbf0f1e08b 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1666,7 +1666,7 @@ class Query(BaseExpression):
             filter_expr = (filter_lhs, OuterRef(filter_rhs.name))
         # Generate the inner query.
         query = Query(self.model)
-        query._filtered_relations = self._filtered_relations
+        query._filtered_relations = {}
         query.add_filter(filter_expr)
```

**分类**：🔴 必须替换（与 A 重复）

**理由**：与 Group A 完全相同的 diff，重复 mutation，必须设计新的不同位置的 mutation。

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index 08d7faf194..cc9344bfdc 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1667,6 +1667,7 @@ class Query(BaseExpression):
         # Generate the inner query.
         query = Query(self.model)
         query._filtered_relations = self._filtered_relations
+        query.annotations = self.annotations.copy()
         query.add_filter(filter_expr)
         query.clear_ordering(True)
         # Try to have as simple as possible subquery -> trim leading joins from
```

**变异语义**：在内层子查询中注入父查询的 annotations（包括 `book_alice` FilteredRelation 注解表达式）。`names_to_path()` 中 `annotation_select` 检查优先于 `_filtered_relations` 检查（第1416-1419行）：`elif name in self._filtered_relations and pos == 0`，但 annotation 检查在前。若内层查询的 `annotation_select` 包含了 `book_alice`，则它会被解析为 annotation 而非 FilteredRelation，返回错误的 `output_field`，导致 JOIN 构建错误或 SQL 生成错误。该 mutation 在只有简单 filter 的测试中通常不影响（父查询无多余注解），但在有多个注解的复杂场景下会导致错误的子查询 SQL。

---

### Group D — 新增

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index 08d7faf194..405f54774c 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -2141,13 +2141,9 @@ class Query(BaseExpression):
             join_field.foreign_related_fields[0].name)
         trimmed_prefix = LOOKUP_SEP.join(trimmed_prefix)
         # Lets still see if we can trim the first join from the inner query
-        # (that is, self). We can't do this for:
-        # - LEFT JOINs because we would miss those rows that have nothing on
-        #   the outer side,
-        # - INNER JOINs from filtered relations because we would miss their
-        #   filters.
-        first_join = self.alias_map[lookup_tables[trimmed_paths + 1]]
-        if first_join.join_type != LOUTER and not first_join.filtered_relation:
+        # (that is, self). We can't do this for LEFT JOINs because we would
+        # miss those rows that have nothing on the outer side.
+        if self.alias_map[lookup_tables[trimmed_paths + 1]].join_type != LOUTER:
             select_fields = [r[0] for r in join_field.related_fields]
             select_alias = lookup_tables[trimmed_paths + 1]
             self.unref_alias(lookup_tables[trimmed_paths])
```

**变异语义**：还原 `trim_start` 中的 `filtered_relation` 检查，使带有 FilteredRelation 的 INNER JOIN 在生成 exclude 子查询时被错误裁剪。裁剪操作（`unref_alias`）会移除 JOIN 节点，导致 FilteredRelation 中的 `ON` 条件（如 `book__title__iexact='poem by alice'`）从子查询中消失。子查询变成了不带 title 过滤条件的 `NOT IN`，结果集出现本不该存在的行。`test_with_join` 仍可通过（filter 路径不走 `trim_start`），但 `test_with_exclude` 因子查询缺少过滤条件而返回空集或错误集合。

---

### Group E — 新增

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index 08d7faf194..16b454b5c7 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -327,7 +327,7 @@ class Query(BaseExpression):
         if 'subq_aliases' in self.__dict__:
             obj.subq_aliases = self.subq_aliases.copy()
         obj.used_aliases = self.used_aliases.copy()
-        obj._filtered_relations = self._filtered_relations.copy()
+        obj._filtered_relations = self._filtered_relations
         # Clear the cached_property
         try:
             del obj.base_table
```

**变异语义**：在 `clone()` 中，克隆后的 Query 对象与原 Query 共享同一个 `_filtered_relations` 字典引用。当 Django 内部对 QuerySet 进行链式操作（filter、annotate、exclude 等）时，每次操作都会调用 `chain()` → `clone()`。若后续操作（如 `build_filtered_relation_q`、`add_filtered_relation`）修改了克隆查询的 `_filtered_relations`，会同时污染原始查询的状态。具体到 `test_with_exclude`：`annotate()` 之后接 `exclude()` 会触发至少一次 clone，若克隆后的内层查询的 `_filtered_relations` 被修改（如添加新的 FilteredRelation 条目），原始查询也受影响，在后续的 `build_filter` 中会遇到重复或错误的映射。这种跨查询状态污染在单次执行中可能不触发，但在测试数据库中复用 TestCase 的情况下或在链式查询场景下（`test_multiple_times` 类测试）会导致不确定性错误。

## 新设计 Mutation 说明

### Group A（重新设计）
基于对 `split_exclude` 初始化流程的分析：`add_filter` 内部立即调用 `names_to_path`，需要 `_filtered_relations` 已就位。将赋值移到 `add_filter` 之后，模拟了开发者认为"先建查询，用完再补配置"的错误直觉。修改位置在 `split_exclude` 函数体内，与原始 bug 的修复点高度相关，但不是简单还原。

### Group C（重新设计）
基于对 `names_to_path` 中优先级顺序的分析（第1416行：先检查 `annotation_select`，再检查 `_filtered_relations`）。向内层子查询注入父查询的 annotations，使 FilteredRelation 名称可能被错误地解析为 annotation，改变了 JOIN 的构建方式。这模拟了开发者认为"子查询也应继承父查询的注解上下文"的错误理解。

### Group D（新增）
直接针对 golden patch 的第二个修复点（`trim_start`），还原了对 `filtered_relation` 属性的检查。该函数专门用于生成 exclude 子查询的 JOIN 裁剪，与 fix1 修改的位置完全不同（fix1 在 `split_exclude`，fix2 在 `trim_start`），确保5个 mutation 修改位置多样。

### Group E（新增）
针对 `clone()` 中的防御性拷贝，去掉 `.copy()` 使两个 Query 实例共享 `_filtered_relations` dict。这是一类典型的"忘记深拷贝可变状态"的真实开发者错误，在简单单次测试中可能无法触发，只在链式操作或多次克隆场景下暴露，具有较高的隐蔽性。
