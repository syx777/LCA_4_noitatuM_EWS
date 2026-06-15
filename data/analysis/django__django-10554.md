# django__django-10554

## 问题背景

当对 union queryset 使用 `values_list()` 并带有 ORDER BY 时，若 ORDER BY 的字段（如 `pk`）不在 `values_list()` 指定的字段列表中，Django 会抛出 `DatabaseError: ORDER BY term does not match any column in the result set`。

根本原因：在 `get_order_by()` 中，当处理 combinator 查询（UNION/INTERSECT/DIFFERENCE）时，代码尝试将 ORDER BY 字段映射到 SELECT 列表中的某一列（用数字位置代替列名）。若该字段不在现有 SELECT 列表中，旧代码直接抛出异常；而正确做法是将该字段追加到 SELECT 列表（通过新增 `add_select_col` 方法），然后用 `len(self.query.select)` 作为该列的位置编号。

## Golden Patch 语义分析

Golden patch 做了两件事：

1. **在 `query.py` 中新增 `add_select_col(self, col)` 方法**：同时向 `self.select` 追加列对象，向 `self.values_select` 追加列名。这两个字段必须保持同步——`select` 是实际 SQL 列列表，`values_select` 是 Python 值映射所用的字段名列表。

2. **在 `compiler.py` 的 `get_order_by()` 中修改 else 分支**：原来只要 ORDER BY 列不在 SELECT 中就抛异常；修复后先检查是否有 `col_alias`（有别名时才抛异常，因为有别名的列本应在 SELECT 中），否则调用 `add_select_col(src)` 将该列追加进 SELECT，然后用 `len(self.query.select)`（追加后的长度）作为 SQL 位置引用。

**为什么 `len(self.query.select)` 是正确的**：追加前 select 长度为 N，追加后为 N+1，因此新列在 SQL 中的位置是 `N+1 = len(self.query.select)`（1-indexed）。

## 调用链分析

```
QuerySet.values_list() / .order_by()
  └─ Query.set_values()          # 设置 values_select 和 select（通过 add_fields）
       └─ Query.add_fields()     # 调用 set_select(cols)
  
SQLCompiler.as_sql()
  └─ pre_sql_setup()
       └─ get_order_by()         # ← 核心修复点
            └─ Query.add_select_col(col)  # ← 新增方法
  └─ get_combinator_sql()
       └─ compiler.query.set_values(...)  # 给子查询同步 values 设置
            └─ Query.add_fields()
```

**数据流**：`values_select` 同时用于：
- `get_combinator_sql()` 的条件判断（`if not compiler.query.values_select`）
- `set_values()` 调用时传入的字段列表
- Python 层面将 SQL 结果映射为字典/元组时的字段名列表

因此 `select` 与 `values_select` 必须始终保持长度和顺序一致。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 高质量（多行、状态污染） | 保留 | clone 在 set_values 之后执行，导致 set_values 修改原始 query，第二次执行 union 时出错 |
| B | 新设计（跨函数，接口契约变异） | 新增 | annotation_select 被从 combined 查询的 set_values 调用中移除 |
| C | 新设计（off-by-one 位置引用） | 新增 | add_select_col 后用 len-1 而非 len，导致 ORDER BY 引用错误列位置 |
| D | 🔴 必须替换（含人工注释 "# Bug:"） | 替换 | 注释直接说明是 bug，极不自然 |
| E | 🔴 必须替换（虚假 strict_order_by 属性） | 替换 | 引用不存在的属性，代码风格明显异常 |

语义浅层共 0 个，替换其中最弱的 floor(0/2) = 0 个：无。
必须替换 2 个（D, E），新增 2 个（B, C）。

## 各组 Mutation 分析

### Group A — 保留
**原 mutation**：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index a14f1254aa..6259da1e8a 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -431,12 +431,12 @@ class SQLCompiler:
                 if not compiler.query.values_select and self.query.values_select:
-                    compiler.query = compiler.query.clone()
                     compiler.query.set_values((
                         *self.query.extra_select,
                         *self.query.values_select,
                         *self.query.annotation_select,
                     ))
+                    compiler.query = compiler.query.clone()
```
**分类**：🟢 保留
**理由**：将 `clone()` 移到 `set_values()` 之后执行。`set_values()` 会就地修改 `compiler.query`，但此时 `compiler.query` 还指向原始 query 对象（不是克隆），因此 `set_values` 的副作用（修改 `select`/`values_select`/`default_cols` 等字段）会污染原始 query。下次重新执行该 union queryset 时，子查询的 `values_select` 已被修改过，导致行为异常。这是一个多步调用才能暴露的状态管理 bug，代码看起来完全合理（clone 只是顺序不同），审查者极难发现。

**最终 mutation**（保留，与原相同）：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index a14f1254aa..6259da1e8a 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -431,12 +431,12 @@ class SQLCompiler:
                 if not compiler.query.values_select and self.query.values_select:
-                    compiler.query = compiler.query.clone()
                     compiler.query.set_values((
                         *self.query.extra_select,
                         *self.query.values_select,
                         *self.query.annotation_select,
                     ))
+                    compiler.query = compiler.query.clone()
```
**变异语义**：克隆时机错误导致原始 query 被 set_values 污染。第一次执行 union 看似正确，但第二次执行时子查询的 `values_select` 已经被设置，绕过了 `if not compiler.query.values_select` 的条件保护，导致列集不再同步。简单测试（只执行一次查询）会通过，只有在 `union queryset` 被重复使用的测试场景下才失败。

---

### Group B — 新设计
**分类**：🟢 高质量（接口契约变异，annotation_select 同步被破坏）
**理由**：`set_values()` 调用时传入的字段列表决定了 combined 查询中 SELECT 的列集。`annotation_select` 中的注释字段本应与父查询保持同步，去掉后导致带注解的 union 查询缺少注解列，出现列数不匹配。对于无注解的简单查询，此 mutation 与正确代码行为一致，因此简单测试不会发现它。

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index a14f1254aa..f4924227f8 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -435,7 +435,6 @@ class SQLCompiler:
                     compiler.query.set_values((
                         *self.query.extra_select,
                         *self.query.values_select,
-                        *self.query.annotation_select,
                     ))
                 part_sql, part_args = compiler.as_sql()
                 if compiler.query.combinator:
```
**变异语义**：combined 查询的 `set_values` 不再包含 `annotation_select` 中的注解列名，导致含有 `annotate()` 的 union 查询丢失注解字段。无注解的普通 `values_list()` union 查询不受影响，因此大多数简单测试通过，只有同时使用 `annotate()` + `union()` + `values()` 的场景才失败。

---

### Group C — 新设计
**分类**：🟢 高质量（off-by-one 位置引用）
**理由**：`add_select_col(src)` 执行后，新列位于 `self.query.select[-1]`，其 SQL 位置（1-indexed）是 `len(self.query.select)`。将位置改为 `len(self.query.select) - 1` 使 ORDER BY 引用了错误的列（前一列），导致结果排序错误或使用了错误的字段值进行排序。该错误在逻辑上完全合理（像是开发者习惯性写了 `-1` 用于 0-indexed 转换），审查时极难发现。

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index a14f1254aa..c3b9e0e844 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -361,7 +361,7 @@ class SQLCompiler:
                     # Add column used in ORDER BY clause without an alias to
                     # the selected columns.
                     self.query.add_select_col(src)
-                    resolved.set_source_expressions([RawSQL('%d' % len(self.query.select), ())])
+                    resolved.set_source_expressions([RawSQL('%d' % (len(self.query.select) - 1), ())])
             sql, params = self.compile(resolved)
```
**变异语义**：ORDER BY 位置引用比实际追加列的位置小 1（off-by-one）。当新增的 ORDER BY 列是 SELECT 列表的第 N 列时，`len - 1 = N - 1`，ORDER BY 会按第 N-1 列排序，而非正确的第 N 列。结果集可以正常返回，但排序字段错误，只在验证排序结果正确性的测试中失败。

---

### Group D — 替换
**原 mutation**：
```diff
     def add_select_col(self, col):
         self.select += col,
-        self.values_select += col.output_field.name,
+        # Bug: Not updating values_select - breaks state initialization
+        pass
```
**分类**：🔴 必须替换（含人工注释 "# Bug:"，极不自然）
**理由**：注释直接写明 "Bug"，任何代码审查者一眼就能发现。`pass` 替代实际逻辑也非常罕见，完全不符合真实开发者写代码的风格。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index fc08442193..e04dc604fd 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1776,7 +1776,6 @@ class Query(BaseExpression):
 
     def add_select_col(self, col):
         self.select += col,
-        self.values_select += col.output_field.name,
 
     def set_select(self, cols):
         self.default_cols = False
```
**变异语义**：`add_select_col` 只更新 `self.select`（SQL 层面的列列表），不更新 `self.values_select`（Python 层面的字段名映射列表）。这导致当 ORDER BY 追加了一个额外列后，`values_select` 的长度比 `select` 少 1，在 values() 查询中造成列数/名称不匹配，导致 `ProgrammingError: ORDER BY position N is not in select list` 或列名映射错误。代码外观简洁自然（只是少了一行），审查者容易忽略。

---

### Group E — 替换
**原 mutation**：
```diff
-                    if getattr(self.query, 'strict_order_by', False):
-                        self.query.add_select_col(src)
-                        resolved.set_source_expressions([RawSQL('%d' % len(self.query.select), ())])
```
**分类**：🔴 必须替换（引用不存在的虚假属性 `strict_order_by`，代码极不自然）
**理由**：`strict_order_by` 属性在整个 Django 代码库中不存在，`getattr(..., False)` 使其永远返回 False，实际上完全禁用了修复。这是非常明显的人工痕迹，任何对 Django 源码有基本了解的开发者都会立即发现该属性不存在。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index a14f1254aa..a16abde5b8 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -425,12 +425,12 @@ class SQLCompiler:
         parts = ()
-        for compiler in compilers:
+        for idx, compiler in enumerate(compilers):
             try:
                 # If the columns list is limited, then all combined queries
                 # must have the same columns list. Set the selects defined on
                 # the query on all combined queries, if not already set.
-                if not compiler.query.values_select and self.query.values_select:
+                if not compiler.query.values_select and self.query.values_select and idx > 0:
                     compiler.query = compiler.query.clone()
                     compiler.query.set_values((
                         *self.query.extra_select,
```
**变异语义**：`idx > 0` 条件使得第一个子查询（`idx == 0`）在 `values_list()` union 中永远不会调用 `set_values()`，即第一个子查询的列集不会被同步。结果第一个子查询的 SELECT 包含所有字段，而后续子查询只包含 `values_list()` 指定的字段，导致列数不一致，SQL 执行失败。这看起来像是开发者想要"只对 idx > 0 的子查询做同步"的合理直觉（认为第一个 compiler 应该保持原样），实际上是错误的。

## 新设计 Mutation 说明

### Group B（新增）
**代码分析基础**：`get_combinator_sql()` 在 `compiler.query.set_values()` 调用时传入父查询的 `extra_select + values_select + annotation_select`。这三者分别代表额外字段、普通值字段和注解字段。如果去掉 `annotation_select`，含有 `annotate()` 的 union 查询的子查询列集就会少于父查询期望的列集。

**位置选择理由**：修改点在 `get_combinator_sql()` 中的 `set_values()` 调用参数，而非 `get_order_by()`——两个 golden fix 函数都被利用，但修改位置不同于 Group A（A 改的是 clone 时机，B 改的是 set_values 参数）。

**模拟真实错误**：真实开发者在阅读 `set_values((*extra, *values, *annotations))` 时，可能误以为 combined 查询的 SELECT 只需要 `extra + values`，而注解字段会在查询执行时自动附加，从而遗漏 `*annotation_select`。

### Group C（新增）
**代码分析基础**：`add_select_col(src)` 将 `src` 追加到 `self.select` 末尾（Python tuple 拼接），追加后 `len(self.select)` 就是新列的 1-indexed SQL 位置。使用 `len - 1` 会引用追加前的最后一列位置（即前一列）。

**位置选择理由**：修改点在 `get_order_by()` 中调用 `add_select_col` 之后的位置引用计算，与 Group A（修改 `get_combinator_sql`）和 Group D（修改 `add_select_col` 函数体）的位置都不重叠。

**模拟真实错误**：真实开发者习惯将 0-indexed 长度转换为索引时写 `len - 1`，但此处需要 1-indexed 位置（SQL 中 ORDER BY 列位置从 1 开始），`len` 本身就是正确答案，`len - 1` 是常见的 off-by-one 失误。
