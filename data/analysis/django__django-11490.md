# django__django-11490

## 问题背景

当对组合查询（UNION/INTERSECTION/DIFFERENCE）调用 `values()` 或 `values_list()` 时，如果同一个基础 queryset (`qs1`) 被多次用于构建组合查询并指定不同的列列表，第二次及之后的调用会返回第一次调用时的列列表，而不是新指定的列列表。

例如：
```python
qs1 = ReservedName.objects.all()
print(qs1.union(qs1).values_list('name', 'order').get())  # ('a', 2) ✓
print(qs1.union(qs1).values_list('order').get())           # ('a', 2) ✗ 应该是 (2,)
```

根本原因：`qs1.union(qs1)` 构建的组合 queryset 中，`combined_queries = (qs1.query, qs1.query)`，两个条目都指向同一个 `qs1.query` 对象。在 `get_combinator_sql` 中，若不克隆子查询的 query 对象，直接调用 `compiler.query.set_values(...)` 会永久改变 `qs1.query.values_select`。后续调用时，条件 `not compiler.query.values_select` 为 False，`set_values` 不再被调用，子查询使用的是上次变异后的列列表。

## Golden Patch 语义分析

```diff
+                    compiler.query = compiler.query.clone()
                     compiler.query.set_values((
                         *self.query.extra_select,
                         *self.query.values_select,
                         *self.query.annotation_select,
                     ))
```

**修复的核心逻辑**：在调用 `set_values()` 之前，先将 `compiler.query`（即指向 `qs1.query` 的引用）替换为其克隆对象。这样 `set_values()` 修改的是克隆对象，而不是 `combined_queries` 中共享的原始 `qs1.query`。每次执行 `get_combinator_sql` 时，都从原始（未变异的）`qs1.query` 出发创建新克隆，确保列选择始终正确传播。

## 调用链分析

```
QuerySet.union(qs1)
  → _combinator_query('union', qs1)
      → combined_queries = (self.query, qs1.query)   # 直接引用，不复制
      
QuerySet.values_list('order').get()
  → SQLCompiler.as_sql()
      → get_combinator_sql('union', False)
          → for compiler in compilers:
              → [条件] not compiler.query.values_select and self.query.values_select
              → [Fix] compiler.query = compiler.query.clone()   ← 关键行
              → compiler.query.set_values(('order',))            ← 修改克隆
              → compiler.as_sql()                                ← 生成子查询 SQL
```

**数据流向**：
- `self.query.values_select` = 外层组合查询指定的列列表（如 `('order',)`）
- `compiler.query.values_select` = 子查询（`qs1.query`）的列列表（初始为 `()`）
- `set_values()` 调用 `clear_select_fields()` + `add_fields()`，会修改 `select`、`values_select`、`default_cols` 等多个属性

**Query.clone() 深拷贝的关键字段**：`alias_refcount`、`alias_map`、`external_aliases`、`table_map`、`where`、`annotations` 等。`values_select`、`select`、`default_cols` 通过赋值隔离，不需要深拷贝。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 新设计（原缺失） | 替换（新增） | 原 mutations.jsonl 中不存在 A 组，新设计高质量 mutation |
| B | 🔴 必须替换 | 替换 | 直接删除 clone() 行，是 golden patch 的直接逆操作 |
| C | 🔴 必须替换 | 替换 | `is None` 检查永远为 False（values_select 始终是 tuple），功能等价于移除整个 if 块 |
| D | 新设计（原缺失） | 替换（新增） | 原 mutations.jsonl 中不存在 D 组，新设计高质量 mutation |
| E | 🔴 必须替换 | 替换 | 含有明显人工痕迹（`# E1: Bug - commented out clone to break values_select`），代码审查立即可见 |

语义浅层共 0 个（B/C/E 均为必须替换），全部替换。

## 各组 Mutation 分析

### Group A — 替换（新设计）

**分类**：🟢 新设计高质量 mutation

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index a791908caf..e53c154e85 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -426,12 +426,12 @@ class SQLCompiler:
                 # must have the same columns list. Set the selects defined on
                 # the query on all combined queries, if not already set.
                 if not compiler.query.values_select and self.query.values_select:
-                    compiler.query = compiler.query.clone()
                     compiler.query.set_values((
                         *self.query.extra_select,
                         *self.query.values_select,
                         *self.query.annotation_select,
                     ))
+                    compiler.query = compiler.query.clone()
                 part_sql, part_args = compiler.as_sql()
```

**变异语义**：将 `clone()` 移至 `set_values()` 之后。`set_values()` 先在原始 `qs1.query` 上执行（永久改变其 `values_select`），然后基于已变异的对象创建克隆用于 SQL 生成。代码审查者会看到 clone 存在而忽略顺序错误。第一次调用 P2P 测试通过（SQL 从克隆生成，列正确），但 `qs1.query` 已被污染；后续不同列列表的 F2P 调用因条件为 False 而失败。

**为何难以发现**：clone() 明确存在，逻辑结构完整，仅执行顺序略有偏移，需要理解 clone 的时序语义才能察觉问题。

---

### Group B — 替换

**原 mutation**：
```diff
@@ -426,7 +426,6 @@ class SQLCompiler:
                 if not compiler.query.values_select and self.query.values_select:
-                    compiler.query = compiler.query.clone()
                     compiler.query.set_values((
```

**分类**：🔴 必须替换（直接冗余）

**理由**：直接删除 `compiler.query = compiler.query.clone()` 一行，是 golden patch 的精确逆操作，完全还原了原始 bug。

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index a791908caf..ab6699b534 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -426,7 +426,7 @@ class SQLCompiler:
                 # must have the same columns list. Set the selects defined on
                 # the query on all combined queries, if not already set.
                 if not compiler.query.values_select and self.query.values_select:
-                    compiler.query = compiler.query.clone()
+                    query = compiler.query.clone()
                     compiler.query.set_values((
                         *self.query.extra_select,
                         *self.query.values_select,
```

**变异语义**：将 `compiler.query = ...` 改为 `query = ...`，克隆被赋值到局部变量 `query`（该变量在第 417 行 `for query, compiler in zip(...)` 循环后仍在作用域内）。克隆对象被丢弃，`set_values()` 仍在原始 `qs1.query` 上执行。代码看起来有 clone，实际上没有生效。P2P 测试靠"恰好列列表相同"侥幸通过，F2P 因列列表不同而失败。

---

### Group C — 替换

**原 mutation**：
```diff
-                if not compiler.query.values_select and self.query.values_select:
+                if compiler.query.values_select is None and self.query.values_select:
```

**分类**：🔴 必须替换（功能等价冗余）

**理由**：`values_select` 初始化为 `()` 空元组（见 query.py 第 205 行），永远不为 `None`。`is None` 检查恒为 False，导致整个 if 块变为死代码，功能等价于完全移除修复。

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index a791908caf..63ced0d64a 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -426,12 +426,14 @@ class SQLCompiler:
                 # must have the same columns list. Set the selects defined on
                 # the query on all combined queries, if not already set.
                 if not compiler.query.values_select and self.query.values_select:
+                    _orig = compiler.query
                     compiler.query = compiler.query.clone()
                     compiler.query.set_values((
                         *self.query.extra_select,
                         *self.query.values_select,
                         *self.query.annotation_select,
                     ))
+                    _orig.values_select = compiler.query.values_select
                 part_sql, part_args = compiler.as_sql()
```

**变异语义**：保存原始查询引用 `_orig`，正确克隆并调用 `set_values()`（SQL 生成正确），最后将新 `values_select` 写回原始对象 `_orig`（即 `qs1.query`）。模拟"保持原始与克隆一致性"的开发错误。第一次调用 SQL 正确，但 `qs1.query.values_select` 被污染；F2P 不同列的调用条件为 False 而失败。极难在代码审查中发现，因为克隆和 set_values 都正确存在。

---

### Group D — 替换（新设计）

**分类**：🟢 新设计高质量 mutation

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index a791908caf..93834f4415 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -425,8 +425,7 @@ class SQLCompiler:
                 # If the columns list is limited, then all combined queries
                 # must have the same columns list. Set the selects defined on
                 # the query on all combined queries, if not already set.
-                if not compiler.query.values_select and self.query.values_select:
-                    compiler.query = compiler.query.clone()
+                if compiler.query.default_cols and self.query.values_select:
                     compiler.query.set_values((
                         *self.query.extra_select,
                         *self.query.values_select,
```

**变异语义**：两处修改：① 将条件从 `not compiler.query.values_select` 改为 `compiler.query.default_cols`；② 移除 clone。`default_cols = True` 初始时，`set_values()` 会调用 `clear_select_fields()` 将其设为 False。因无 clone，`qs1.query.default_cols` 在第一次调用后变为 False，后续调用条件为 False，`set_values` 不再执行。多行修改，`default_cols` 作为替代检查属性具有语义合理性（初始状态确实意味着"尚未应用 values 限制"），难以在代码审查中察觉。

---

### Group E — 替换

**原 mutation**：
```diff
-                    compiler.query = compiler.query.clone()
+                    # E1: Bug - commented out clone to break values_select
```

**分类**：🔴 必须替换（不自然，明显人工痕迹）

**理由**：注释直接写明这是一个 "Bug"，任何代码审查者一眼即可识别。

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index a791908caf..d3dc81af03 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -426,7 +426,8 @@ class SQLCompiler:
                 # must have the same columns list. Set the selects defined on
                 # the query on all combined queries, if not already set.
                 if not compiler.query.values_select and self.query.values_select:
-                    compiler.query = compiler.query.clone()
+                    if self.query.annotation_select:
+                        compiler.query = compiler.query.clone()
                     compiler.query.set_values((
                         *self.query.extra_select,
                         *self.query.values_select,
```

**变异语义**：仅在外层查询有 annotation_select 时才执行 clone。对于无注解的简单 union 查询（`annotation_select = {}`，为空 dict，falsy），clone 永远不执行，`set_values` 直接修改原始 `qs1.query`。模拟"性能优化"思路——仅在必要时（有注解时）才克隆。P2P 测试通过（qs1 无注解但首次调用相同列列表侥幸通过），F2P 因不同列列表失败。表面上是针对注解场景的保护性优化，实则破坏了基础场景。

## 新设计 Mutation 说明

**Group A（clone after set_values）**：
基于对 Django clone() 时序语义的深入分析——`set_values()` 修改的是它被调用的对象，clone 必须在 set_values 之前执行才能保护原始对象。该变异将 clone 位置下移一行，制造了一个极具迷惑性的顺序错误，真实开发者在重构或移动代码时可能犯此类错误。

**Group D（default_cols + no clone）**：
`default_cols` 是 Query 对象中与"是否已应用值限制"相关的另一个属性（通过 `clear_select_fields()` 被 `set_values` 置为 False），可替代 `values_select` 作为检测"尚未设置列限制"的条件。此变异同时更改条件属性并移除 clone，多行修改使得 code review 需要同时检查两处才能理解 bug，增加了难度。
