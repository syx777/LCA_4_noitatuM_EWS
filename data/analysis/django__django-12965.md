# django__django-12965

## 问题背景

`Model.objects.all().delete()` 在 Django 3.1 中产生了性能回归：原本生成简单的 `DELETE FROM table`，变成了 `DELETE FROM table WHERE id IN (SELECT id FROM table)`，在 MySQL/MariaDB 上性能差距高达 37 倍，同时还因为子查询中表名重复而无法与 `LOCK TABLES` 共用。

根本原因：`SQLDeleteCompiler.single_alias` 属性检查 `alias_map` 中引用计数 > 0 的别名数量是否为 1，但对于 `all().delete()` 这类没有过滤条件的查询，`alias_map` 在计算 `single_alias` 时可能为空（`get_initial_alias()` 尚未被调用），导致 `sum() == 1` 为 False，错误地走向子查询路径。

## Golden Patch 语义分析

**修改前**：
```python
@cached_property
def single_alias(self):
    return sum(self.query.alias_refcount[t] > 0 for t in self.query.alias_map) == 1
```
若 `alias_map` 为空（无条件全删），`sum()` 为 0，`0 == 1` 为 False → `single_alias = False` → 走子查询。

**修改后**：
```python
@cached_property
def single_alias(self):
    # Ensure base table is in aliases.
    self.query.get_initial_alias()
    return sum(self.query.alias_refcount[t] > 0 for t in self.query.alias_map) == 1
```
先调用 `get_initial_alias()` 确保基础表已注册到 `alias_map`（`alias_refcount[base_table] = 1`），再检查活跃别名数是否为 1。无条件全删时：`alias_map = {base_table: ...}`, `sum(>0) = 1 == 1 = True` → `single_alias = True` → 直接 `DELETE FROM table`。

## 调用链分析

```
QuerySet.delete()
  └── Collector.delete()
      └── SQLDeleteCompiler.as_sql()          # compiler.py L1421
          └── self.single_alias               # cached_property
              └── self.query.get_initial_alias()  # query.py L916
                  → 若 alias_map 为空: join(BaseTable(...)) → alias_refcount[base_table] = 1
                  → 若 alias_map 非空: ref_alias(base_table) → alias_refcount[base_table] += 1
              └── sum(alias_refcount[t] > 0 for t in alias_map)
              → == 1: True → self._as_sql(self.query)   # 直接 DELETE FROM
              → != 1: False → 子查询路径
```

`get_initial_alias()` 的关键行为：
- `alias_map` 为空时：调用 `join(BaseTable(...))` 注册基础表，`alias_refcount[base_table] = 1`
- `alias_map` 非空时：调用 `ref_alias(base_table)` 将 `alias_refcount[base_table] += 1`

`single_alias` 是 `@cached_property`：首次访问时计算并缓存，因此 `get_initial_alias()` 只会在第一次访问时运行。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | `if False:` 硬编码，明显人工痕迹 |
| B | （缺失） | 新设计 | 无原 mutation，设计全新 mutation |
| C | 🟢 保留 | 保留 | `if where:` → `if where is not None:`，不同函数中的微妙真值检查变更 |
| D | （缺失） | 新设计 | 无原 mutation，设计全新 mutation |
| E | 🔴 必须替换 | 替换 | 添加 `use_optimization=False` 参数并使优化默认关闭，不自然且不符合调用约定 |

必须替换 2 个（A、E），新设计 2 个（B、D）。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：
```diff
-        if self.single_alias:
+        if False:
```
**分类**：🔴 必须替换
**理由**：`if False:` 是明显的人工注入痕迹，任何代码审查都会立刻发现。
**最终 mutation**（替换，跨文件变异到 `query.py`）：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index ce18098fd2..71134158c6 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -920,7 +920,7 @@ class Query(BaseExpression):
         """
         if self.alias_map:
             alias = self.base_table
-            self.ref_alias(alias)
+            self.unref_alias(alias)
         else:
             alias = self.join(BaseTable(self.get_meta().db_table, None))
         return alias
```
**变异语义**：`get_initial_alias()` 在 `alias_map` 非空时调用 `unref_alias` 而非 `ref_alias`。`ref_alias` 将 refcount 加 1，`unref_alias` 将 refcount 减 1。Golden patch 的 fix 依赖 `get_initial_alias()` 将基础表 refcount 提升到 1（从 0 开始），使 `sum(>0) = 1`。若调用 `unref_alias`，refcount 从 0 变为 -1（或从 1 变为 0），`sum(refcount > 0)` 为 0，`0 == 1` 为 False → single_alias = False → 仍走子查询路径。跨文件变异（`query.py`），代码审查仅看 `compiler.py` 时完全看不出问题，模拟了开发者混淆"增加引用" vs "减少引用"的方向错误。

---

### Group B — 新设计
**原 mutation**：（缺失，全新设计）
**最终 mutation**：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index a1487c4f95..91b6ffc9ea 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -1409,7 +1409,7 @@ class SQLDeleteCompiler(SQLCompiler):
     def single_alias(self):
         # Ensure base table is in aliases.
         self.query.get_initial_alias()
-        return sum(self.query.alias_refcount[t] > 0 for t in self.query.alias_map) == 1
+        return len(self.query.alias_map) == 1
```
**变异语义**：将"活跃别名数（refcount > 0）== 1"改为"alias_map 中条目数 == 1"。通常情况下，两者等价：简单查询只有基础表，复杂查询有多个表。但存在一个重要差异：`alias_map` 中可能包含 refcount 为 0 的别名（已 unref 但未从 map 中移除的表）。对于某些复杂删除查询（如曾经 join 了额外表但后来减引用），`alias_map` 可能有 2 个条目但只有 1 个活跃 → `sum(>0) == 1` 为 True（正确优化），而 `len == 1` 为 False（错误地走子查询）。模拟了开发者"简化"代码时忽略了 refcount 语义的错误。

---

### Group C — 保留
**原 mutation**：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index a1487c4f95..e8cc8d73c8 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -1416,7 +1416,7 @@ class SQLDeleteCompiler(SQLCompiler):
             'DELETE FROM %s' % self.quote_name_unless_alias(query.base_table)
         ]
         where, params = self.compile(query.where)
-        if where:
+        if where is not None:
             result.append('WHERE %s' % where)
         return ' '.join(result), tuple(params)
```
**分类**：🟢 保留
**理由**：修改在 `_as_sql` 函数（与 golden patch 修改的 `single_alias` 属于不同方法），且改变了 `where` 的真值判断语义。`if where:` 在 `where` 为空字符串 `""` 时为 False（不追加 WHERE），而 `if where is not None:` 对空字符串为 True（追加 `WHERE`，产生非法 SQL）。当 `compile(query.where)` 返回空字符串时（如 WHERE 子句存在但编译为空），此 mutation 会产生语法错误的 DELETE 语句。不同函数、微妙真值语义变更，属于高质量 mutation。
**最终 mutation**（与原相同）：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index a1487c4f95..e8cc8d73c8 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -1416,7 +1416,7 @@ class SQLDeleteCompiler(SQLCompiler):
             'DELETE FROM %s' % self.quote_name_unless_alias(query.base_table)
         ]
         where, params = self.compile(query.where)
-        if where:
+        if where is not None:
             result.append('WHERE %s' % where)
         return ' '.join(result), tuple(params)
```
**变异语义**：`compile(query.where)` 返回 `(where_str, params)` 其中 `where_str` 是 SQL 条件字符串。正常情况下无条件查询返回 `("", [])` — 空字符串为 falsy，`if where` 为 False，不追加 WHERE（正确）。`if where is not None` 对 `""` 为 True，追加 `WHERE ` 产生 `DELETE FROM table WHERE`（语法错误）。影响 `_as_sql` 被调用的所有路径（single_alias=True 的情形），只有在无 WHERE 条件时才触发，因为有条件时 `where` 非空字符串，两者均为 True。

---

### Group D — 新设计
**原 mutation**：（缺失，全新设计）
**最终 mutation**：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index a1487c4f95..4ef94301da 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -1407,9 +1407,10 @@ class SQLInsertCompiler(SQLCompiler):
 class SQLDeleteCompiler(SQLCompiler):
     @cached_property
     def single_alias(self):
+        result = sum(self.query.alias_refcount[t] > 0 for t in self.query.alias_map) == 1
         # Ensure base table is in aliases.
         self.query.get_initial_alias()
-        return sum(self.query.alias_refcount[t] > 0 for t in self.query.alias_map) == 1
+        return result
```
**变异语义**：将 `get_initial_alias()` 调用移到 `sum()` 计算之后。`single_alias` 是 `@cached_property`，首次访问时执行。`result` 在 `get_initial_alias()` 之前计算：若 `alias_map` 为空（`all().delete()` 情形），`sum()=0 != 1 → result=False`；然后 `get_initial_alias()` 运行注册基础表（side effect），但 `result` 已经是 False。`single_alias` 返回 False → 走子查询路径。Fix 的核心思想是"先确保别名存在，再检查数量"，此 mutation 颠倒了顺序，使初始化的 side effect 在检查之后才发生。代码读起来只是"变量提取重构"，逻辑顺序的微妙差异需要深入理解才能发现。

---

### Group E — 替换
**原 mutation**：
```diff
-    def as_sql(self):
+    def as_sql(self, use_optimization=False):
     ...
-        if self.single_alias:
+        if use_optimization and self.single_alias:
```
**分类**：🔴 必须替换
**理由**：新增参数 `use_optimization=False` 默认关闭优化，且没有任何调用方会传 `True`，因此优化路径永远不会执行。该 mutation 引入了函数签名变更，且参数名 `use_optimization` 带有明显"功能开关"含义，代码审查会立刻注意到。
**最终 mutation**（替换，`== 1` → `>= 1`）：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index a1487c4f95..17467b0ba3 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -1409,7 +1409,7 @@ class SQLDeleteCompiler(SQLCompiler):
     def single_alias(self):
         # Ensure base table is in aliases.
         self.query.get_initial_alias()
-        return sum(self.query.alias_refcount[t] > 0 for t in self.query.alias_map) == 1
+        return sum(self.query.alias_refcount[t] > 0 for t in self.query.alias_map) >= 1
```
**变异语义**：将"活跃别名数精确等于 1"改为"活跃别名数至少为 1"。对简单的 `all().delete()`：活跃别名=1，`1 >= 1 = True`（与 fix 相同）。对带 JOIN 的复杂删除查询（如通过 related_objects 的级联删除，活跃别名=2）：`2 >= 1 = True`（错误！应为 False）→ `single_alias=True` → 直接调用 `_as_sql(self.query)`，但 `self.query` 包含 JOIN 条件，`_as_sql` 中的 `compile(query.where)` 可能引用多个表的条件，产生语义错误的 DELETE 语句或 SQL 错误。`test_fast_delete_all` 通过（单表删除），其他多表关联删除测试可能失败。

## 新设计 Mutation 说明

### Group A 新设计依据
Golden patch 的核心 side effect 在 `get_initial_alias()`：当 `alias_map` 非空时调用 `ref_alias` 将 refcount+1，使基础表在 refcount=0 时重新变为"活跃"。`ref_alias` 和 `unref_alias` 是一对镜像操作（+1 vs -1），开发者在阅读时极易混淆方向。跨文件变异使 compiler.py 中的代码逻辑看起来完全正确，只有追踪到 query.py 才能发现 refcount 方向错误。

### Group B 新设计依据
`sum(alias_refcount[t] > 0 for t)` 和 `len(alias_map)` 在大多数场景下等价，但当 alias_map 包含 refcount=0 的"僵尸别名"时行为不同。开发者在"代码简化"时容易将精确的 refcount 检查替换为更简单的 len 检查，忽略了 refcount 的语义意义。此 mutation 在常见测试用例中通过，仅在特定别名管理边界场景下失败。

### Group D 新设计依据
调用顺序的 bug 是真实开发中极为常见的错误："初始化-使用"顺序被颠倒。此处将 `get_initial_alias()` 的初始化 side effect 移到检查之后，从外观看像是开发者进行了"变量提取"重构（将 `return expr` 拆为 `result = expr; ...; return result`），完全符合代码整洁的外观，但语义完全错误。这种"顺序颠倒"类型的 mutation 极难被浅层代码审查发现。
