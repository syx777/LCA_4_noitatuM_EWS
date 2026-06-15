# django__django-12050

## 问题背景

`Query.resolve_lookup_value` 中的变更（来自 #30687）导致 list 类型的输入值被强制转换为 tuple，破坏了依赖输入类型匹配的 ORM 字段（如 `PickledField`）。预期行为是：可迭代对象的返回类型应与输入类型一致（list → list，tuple → tuple）。

Golden patch 对应 git commit `8be79984dc`（"Fixed #30971 -- Prevented Query.resolve_lookup_value() from coercing list values to tuples"），将旧的显式循环替换为使用 `type(value)(...)` 的递归生成器表达式，同时保留了输入类型。

## Golden Patch 语义分析

**修复前（base_commit）**：
```python
resolved_values = []
for sub_value in value:
    if hasattr(sub_value, 'resolve_expression'):
        if isinstance(sub_value, F):
            resolved_values.append(sub_value.resolve_expression(..., simple_col=simple_col))
        else:
            resolved_values.append(sub_value.resolve_expression(...))
    else:
        resolved_values.append(sub_value)
value = tuple(resolved_values)  # ← 始终强制转换为 tuple！
```

**修复后（golden patch）**：
```python
return type(value)(
    self.resolve_lookup_value(sub_value, can_reuse, allow_joins, simple_col)
    for sub_value in value
)
```

核心变化有两点：
1. `tuple(resolved_values)` → `type(value)(...)` — 用 `type(value)` 保留输入容器类型
2. 明确的 F/非F 判断 → 统一使用递归 `resolve_lookup_value` 调用，更简洁也更正确

F2P 测试 `test_iterable_lookup_value`：传入 `Q(name=['a', 'b'])`，期望 `name_exact.rhs == "['a', 'b']"`。这是因为 `Item.name` 是 CharField，Django 会调用 `CharField.get_prep_value(value)` → `str(value)`；list 字符串化为 `"['a', 'b']"`，tuple 字符串化为 `"('a', 'b')"`，两者不同。

## 调用链分析

```
build_where(Q(name=['a', 'b']))
  └─ _add_q(...)
       └─ build_filter(filter_expr, ...)
            ├─ solve_lookup_type('name')     # 解析 lookup 类型
            ├─ resolve_lookup_value(['a','b'], ...)  # ← 核心修复点
            │    └─ isinstance(['a','b'], (list,tuple)) → True
            │         └─ return type(['a','b'])([resolve each sub]) = list
            ├─ isinstance(value, (Iterator, list)) → ??? [E mutation 在此]
            └─ build_lookup(['exact'], col, value)
                 └─ Exact(col, value)
                      └─ get_prep_lookup()
                           └─ CharField.get_prep_value(value) = str(value)
                                → "['a', 'b']" (list) or "('a', 'b')" (tuple)
```

## 替换决策总览

mutations.jsonl 中仅有 B 和 C 两条记录（应有5条），且两者 diff 完全相同（均将 `type(value)(` 改回 `tuple(`），直接还原 golden fix，属于 🔴 必须替换。A、D、E 三组不存在，需全新设计。

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换（不存在） | 替换/新建 | 缺失，需全新设计 |
| B | 🔴 必须替换 | 替换 | 与 C 完全相同 diff，直接还原 golden fix |
| C | 🔴 必须替换 | 替换 | 与 B 完全相同 diff，直接还原 golden fix |
| D | 🔴 必须替换（不存在） | 替换/新建 | 缺失，需全新设计 |
| E | 🔴 必须替换（不存在） | 替换/新建 | 缺失，需全新设计 |

语义浅层共 0 个（两个原有 mutation 均为直接冗余 🔴 级别）。

## 各组 Mutation 分析

### Group A — 替换（新建）
**原 mutation**：不存在
**分类**：需新建

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index dd5889625f..f28d8d6e93 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1059,7 +1059,7 @@ class Query(BaseExpression):
         elif isinstance(value, (list, tuple)):
             # The items of the iterable may be expressions and therefore need
             # to be resolved independently.
-            return type(value)(
+            return (list if isinstance(value, tuple) else tuple)(
                 self.resolve_lookup_value(sub_value, can_reuse, allow_joins, simple_col)
                 for sub_value in value
             )
```
**变异语义**：将类型转换逻辑反转——tuple 输入返回 list，list 输入返回 tuple（即与 golden fix 刚好相反）。看起来像是开发者写了"如果是 tuple 就用 list，否则用 tuple"，条件逻辑互换。API 契约被违反：函数不再保留输入类型。对 F2P 测试：list 输入返回 tuple → `str(('a','b'))` = `"('a', 'b')"` ≠ `"['a', 'b']"` → FAIL。P2P 测试均不涉及列表查询，全部 PASS。

---

### Group B — 替换
**原 mutation**（与 C 相同）：
```diff
-            return type(value)(
+            return tuple(
                 self.resolve_lookup_value(sub_value, can_reuse, allow_joins, simple_col)
                 for sub_value in value
             )
```
**分类**：🔴 必须替换（直接冗余，是 golden patch 的逆操作）

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index dd5889625f..6f1a80ea23 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1059,7 +1059,9 @@ class Query(BaseExpression):
         elif isinstance(value, (list, tuple)):
             # The items of the iterable may be expressions and therefore need
             # to be resolved independently.
-            return type(value)(
+            if not value:
+                return value
+            return tuple(
                 self.resolve_lookup_value(sub_value, can_reuse, allow_joins, simple_col)
                 for sub_value in value
             )
```
**变异语义**：为空迭代器添加了边界守卫（`if not value: return value`，看似合理的防御性代码），但对非空情况仍使用 `tuple(...)` 而非 `type(value)(...)`。开发者可能认为"空列表无需处理，非空情况用 tuple"，忽视了类型保留。非空 list 返回 tuple → F2P FAIL。空 list 处理正确，P2P 全部 PASS（无列表值测试）。

---

### Group C — 替换
**原 mutation**（与 B 相同，此处省略原diff）

**分类**：🔴 必须替换（直接冗余）

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index dd5889625f..28ca19a505 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1059,10 +1059,12 @@ class Query(BaseExpression):
         elif isinstance(value, (list, tuple)):
             # The items of the iterable may be expressions and therefore need
             # to be resolved independently.
-            return type(value)(
-                self.resolve_lookup_value(sub_value, can_reuse, allow_joins, simple_col)
-                for sub_value in value
-            )
+            resolved_values = []
+            for sub_value in value:
+                resolved_values.append(
+                    self.resolve_lookup_value(sub_value, can_reuse, allow_joins, simple_col)
+                )
+            return tuple(resolved_values)
         return value
 
     def solve_lookup_type(self, lookup):
```
**变异语义**：将生成器表达式改回显式循环（更"可读"的写法），但在 `return` 时用了 `tuple(resolved_values)` 而非 `type(value)(resolved_values)`。这模拟了"部分修复"场景：开发者看到需要使用 `resolve_lookup_value` 递归调用（修了一半），却保留了原始的 `tuple()` 类型强制转换（忘了修另一半）。代码审查时，多行改动看起来像是完整修复，难以发现遗漏的 `type(value)` 改动。F2P FAIL（list→tuple），P2P PASS。

---

### Group D — 替换（新建）
**原 mutation**：不存在

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index dd5889625f..7e13519bfd 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1245,6 +1245,8 @@ class Query(BaseExpression):
 
         pre_joins = self.alias_refcount.copy()
         value = self.resolve_lookup_value(value, can_reuse, allow_joins, simple_col)
+        if isinstance(value, list):
+            value = tuple(value)
         used_joins = {k for k, v in self.alias_refcount.items() if v > pre_joins.get(k, 0)}
 
         self.check_filterable(value)
```
**变异语义**：在 `build_filter` 函数中（与主修复点不同的函数），在 `resolve_lookup_value` 之后添加了 list→tuple 的"标准化"代码。模拟了"为下游 SQL 参数处理统一类型"的开发者思路。修改位置在不同函数中，且紧跟 `resolve_lookup_value` 调用，看似合理的后处理。F2P FAIL（list 在进入 `build_lookup` 前被转换为 tuple），P2P PASS（无其他 list 值测试）。

---

### Group E — 替换（新建）
**原 mutation**：不存在

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index dd5889625f..bf5a40189d 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1266,8 +1266,8 @@ class Query(BaseExpression):
             )
 
             # Prevent iterator from being consumed by check_related_objects()
-            if isinstance(value, Iterator):
-                value = list(value)
+            if isinstance(value, (Iterator, list)):
+                value = tuple(value)
         self.check_related_objects(join_info.final_field, value, join_info.opts)
 
             # split_exclude() needs to know which joins were generated for the
```
**变异语义**：在 `build_filter` 的 Iterator 处理代码处做了两处修改：① 将 `isinstance(value, Iterator)` 扩展为 `isinstance(value, (Iterator, list))`（将 list 也纳入"需要转换"的范围）；② 将 `list(value)` 改为 `tuple(value)`（统一转换为不可变 tuple，可能是认为"不可变对象更安全"）。这模拟了开发者将 list 视为"消费型迭代器"而需要"固化"的误解，同时将目标类型从 list 改为 tuple。修改位置完全不同（`try` 块内 `check_related_objects` 之前，而非 `resolve_lookup_value` 内），极难定位。F2P FAIL（list 在此被转换为 tuple，后续 `build_lookup` 收到 tuple），P2P PASS。

---

## 新设计 Mutation 说明

**Group A**：基于对 `type(value)` 调用的分析，设计了条件反转的类型选择：`(list if isinstance(value, tuple) else tuple)`。看起来像是开发者在写类型分支时交换了 list 和 tuple 的对应关系，是一个在代码审查中极不显眼的单行错误。

**Group B**：基于边界条件分析，引入了"空迭代器提前返回"的防御性代码，但后续的类型转换仍用了 `tuple()` 而非 `type(value)()`。模拟了开发者"先加边界守卫，然后用 tuple 处理非空情况"的惯性思维——看似完整的修复，却遗漏了关键的类型保留。

**Group C**：基于对代码演进的理解（旧代码用显式循环，新代码用生成器），设计了"改写为显式循环但保留 `tuple()` 强制转换"的变异。模拟了开发者"提升可读性"的重构行为，在多行修改中隐藏了遗漏 `type(value)` 的 bug，代码审查时最难发现。

**Group D**：通过研究 `build_filter` 调用链，发现 `resolve_lookup_value` 的调用点是注入后处理代码的绝佳位置。添加 `if isinstance(value, list): value = tuple(value)` 紧跟其后，看起来像"SQL 层统一使用 tuple 传参"的约束，实际上完全绕过了 `resolve_lookup_value` 中的类型保留修复。修改在不同函数中，增加了定位难度。

**Group E**：基于对 `build_filter` 中 Iterator 处理逻辑的分析，发现该处代码将 Iterator 转换为 list 以防止被消耗。将 `(Iterator,)` 扩展为 `(Iterator, list)` 并将目标类型改为 `tuple`，看起来像是"防止 list 也被消耗并统一为不可变类型"的误操作。修改发生在 `check_related_objects` 之前的 `try` 块中，与主修复点相距约 200 行，是所有变异中隐蔽性最高的一个。
