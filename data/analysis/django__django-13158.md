# django__django-13158

## 问题背景

`QuerySet.none()` 在 combined queries（union/intersection/difference）上无效，返回所有结果而非空集。原因：`set_empty()` 只在外层查询的 WHERE 中添加 `NothingNode`，但 `combined_queries` 中的子查询未被置空。SQL 编译时，组合查询从各子查询读取数据，外层 WHERE 并不过滤这些子查询，导致 `.none()` 无效。

Golden patch 的两处修复：
1. **`clone()`** 中添加：`obj.combined_queries = tuple(query.clone() for query in self.combined_queries)` — 确保克隆时子查询也被深度克隆，避免状态共享
2. **`set_empty()`** 中添加：`for query in self.combined_queries: query.set_empty()` — 递归置空所有子查询

## Golden Patch 语义分析

```python
# patch 前的 set_empty（有 bug）：
def set_empty(self):
    self.where.add(NothingNode(), AND)  # 只设置外层，子查询不受影响

# patch 后的 set_empty（修复后）：
def set_empty(self):
    self.where.add(NothingNode(), AND)
    for query in self.combined_queries:  # 递归置空所有子查询
        query.set_empty()

# clone() 修复（确保克隆独立性）：
obj.combined_queries = tuple(query.clone() for query in self.combined_queries)
```

`combined_queries` 的结构：当 `qs1.union(qs2)` 时，结果的 `query.combined_queries = (qs1.query, qs2.query)`。`none()` 克隆查询后调用 `set_empty()`，需要同时置空这两个子查询。

## 调用链分析

```
qs3 = qs1.union(qs2)
  → _combinator_query('union', qs2)
      clone.query.combined_queries = (qs1.query, qs2.query)
      clone.query.combinator = 'union'

qs3.none()
  → QuerySet.none() → QuerySet._chain() → query.chain() → query.clone()
      [With fix] obj.combined_queries = (qs1.query.clone(), qs2.query.clone())
  → clone.query.set_empty()
      clone.query.where += NothingNode (AND)
      [With fix] qs1.query.clone().set_empty() → qs1_clone.where += NothingNode
      [With fix] qs2.query.clone().set_empty() → qs2_clone.where += NothingNode
  → SQL: (SELECT num FROM number WHERE 1=0) UNION (SELECT num FROM number WHERE 1=0)
  → Returns: []  ✓
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 缺失 | 新设计 | A 组缺失 |
| B | 缺失 | 新设计 | B 组缺失 |
| C | 缺失 | 新设计 | C 组缺失 |
| D | 🔴 必须替换 | 替换 | 含有人工注释 `# BUG`，明显不自然 |
| E | 🔴 必须替换 | 替换 | 直接删除 set_empty 循环，是 base_commit 的直接还原 |

全部需要重新设计。

## 各组 Mutation 分析

### Group A — 新设计

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index 1623263964..b6adc778a1 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1778,8 +1778,8 @@ class Query(BaseExpression):
 
     def set_empty(self):
         self.where.add(NothingNode(), AND)
-        for query in self.combined_queries:
-            query.set_empty()
+        if self.combined_queries:
+            self.combined_queries[0].set_empty()
 
     def is_empty(self):
         return any(isinstance(c, NothingNode) for c in self.where.children)
```

**分类**：A1（修改循环为只处理第一个元素）

**变异语义**：将对 `combined_queries` 的完整循环改为只对第一个子查询（`[0]`）调用 `set_empty()`。对于 `qs1.union(qs2)`：`combined_queries = (qs1.query, qs2.query)`，只有 `qs1.query` 被置空，`qs2.query` 仍然有效。`UNION` 结果：空集 UNION qs2_results = qs2_results → qs3.none() 仍返回非空结果 → F2P 失败。P2P 安全：非组合查询的 `combined_queries` 为空 → 条件不满足 → 无影响。

---

### Group B — 新设计

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index 1623263964..77fb90af9c 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1779,7 +1779,8 @@ class Query(BaseExpression):
     def set_empty(self):
         self.where.add(NothingNode(), AND)
         for query in self.combined_queries:
-            query.set_empty()
+            if self.combinator != 'union':
+                query.set_empty()
 
     def is_empty(self):
         return any(isinstance(c, NothingNode) for c in self.where.children)
```

**分类**：B2（移除关键情况的处理：对 union 跳过子查询置空）

**变异语义**：只在 combinator 不是 `'union'` 时才递归置空子查询。对 union 查询：跳过子查询的 `set_empty()`，子查询仍有效 → `none()` 后 union 仍返回结果 → F2P 失败。P2P 安全：P2P 测试使用标准查询（`combined_queries=()` → 循环不执行）。看起来像是"intersection/difference 的 none 需要特殊处理，union 不需要"的错误理解。

---

### Group C — 新设计

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index 1623263964..81ee46779e 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -305,7 +305,7 @@ class Query(BaseExpression):
             obj.annotation_select_mask = None
         else:
             obj.annotation_select_mask = self.annotation_select_mask.copy()
-        obj.combined_queries = tuple(query.clone() for query in self.combined_queries)
+        obj.combined_queries = tuple(self.combined_queries)
         # _annotation_select_cache cannot be copied, as doing so breaks the
```

**分类**：C1（共享 combined_queries 对象而非克隆）

**变异语义**：`clone()` 时保留对原始子查询对象的引用而不克隆它们。`qs3.none()` 创建 none 查询时克隆 qs3 的查询，但 `combined_queries` 与 qs3 共享同一对象。当 none 查询的 `set_empty()` 递归置空子查询时，会同时置空 qs3 的原始子查询！导致第二个断言 `assertNumbersEqual(qs3, [0,1,8,9])` 失败（qs3 也变为空集）→ F2P 失败。难以发现：浅拷贝 vs 深克隆对大多数场景无差异，只在连续操作时暴露。

---

### Group D — 替换

**原 mutation**：添加 `obj.is_empty = True`（含 `# BUG` 注释）

**分类**：🔴 必须替换（不自然，含明显人工痕迹）

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index 1623263964..da5835cfc1 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -305,7 +305,7 @@ class Query(BaseExpression):
             obj.annotation_select_mask = None
         else:
             obj.annotation_select_mask = self.annotation_select_mask.copy()
-        obj.combined_queries = tuple(query.clone() for query in self.combined_queries)
+        obj.combined_queries = ()
         # _annotation_select_cache cannot be copied, as doing so breaks the
```

**分类**：D1（克隆时将 combined_queries 初始化为空元组）

**变异语义**：`clone()` 时将 `combined_queries` 设为空元组 `()` 而非克隆原始子查询。none 查询克隆后的 `combined_queries = ()` → `set_empty()` 的循环不执行 → 只有外层 WHERE 被置空 → 子查询未被置空 → UNION 仍从原始子查询获取数据 → `none()` 返回非空结果 → F2P 失败。P2P 安全：非组合查询的 `combined_queries` 本身就是 `()` → 行为无变化。

---

### Group E — 替换

**原 mutation**：删除 `for query in self.combined_queries: query.set_empty()` 循环（直接还原）

**分类**：🔴 必须替换（直接还原 base_commit）

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index 1623263964..9810dd621b 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1779,7 +1779,7 @@ class Query(BaseExpression):
     def set_empty(self):
         self.where.add(NothingNode(), AND)
         for query in self.combined_queries:
-            query.set_empty()
+            query.where.add(NothingNode(), OR)
 
     def is_empty(self):
         return any(isinstance(c, NothingNode) for c in self.where.children)
```

**分类**：E2（使用错误的 OR 连接器而非递归 set_empty）

**变异语义**：对子查询使用 `query.where.add(NothingNode(), OR)` 而非 `query.set_empty()`。`set_empty()` 用 AND 添加 NothingNode：`WHERE (existing) AND (1=0)` → 确保返回空集。本 mutation 用 OR：`WHERE (existing) OR (1=0)` = `WHERE existing` → 子查询行为不变！UNION 仍从子查询中获取正常结果 → `none()` 返回非空 → F2P 失败。P2P 安全：非组合查询 `combined_queries=()` → 循环不执行。难以发现：调用 `query.where.add(NothingNode(), ...)` 看起来是合理的直接操作，但 AND/OR 的差异改变了语义。
