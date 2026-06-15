# django__django-11299

## 问题背景

当使用 `CheckConstraint` 并且约束条件中包含 OR 与 AND 的组合（如 `Q(a=1, b=2) | Q(c=3)`）时，Django 在 SQLite 和 Oracle 上生成了错误的 SQL。问题在于 AND 子句中的字段使用了完全限定名（如 `"new__app_testconstraint"."field_1"`），而 OR 子句中的字段使用了简单列名（如 `"flag"`）。

当 SQLite 执行 rename table 操作后，旧表名已消失，CHECK constraint 中仍然引用旧表名，导致 `malformed database schema` 错误。

Golden patch 修复：在 `_add_q` 方法递归处理子 Q 对象（`Node` 类型的 children）时，将 `simple_col` 参数正确传递下去。

## Golden Patch 语义分析

**修复前（base_commit）**：
```python
child_clause, needed_inner = self._add_q(
    child, used_aliases, branch_negated,
    current_negated, allow_joins, split_subq)
```

**修复后**：
```python
child_clause, needed_inner = self._add_q(
    child, used_aliases, branch_negated,
    current_negated, allow_joins, split_subq, simple_col)
```

核心语义：`build_where` 以 `simple_col=True` 调用 `_add_q`，告知整个查询树"用 `SimpleCol`（不带表名）而非 `Col`（带表名）"。但当 Q 对象有嵌套子 Q 时，递归调用 `_add_q` 没有传 `simple_col`，默认为 `False`，导致嵌套层的叶子节点用 `Col`（带表名）。修复确保 `simple_col` 在整个 Q 对象树的递归中都正确传播。

## 调用链分析

```
CheckConstraint.constraint_sql()
  → compiler.compile(check)
    → build_where(q_object)   # simple_col=True
      → _add_q(q_object, simple_col=True)
          ├─ for leaf children: build_filter(..., simple_col=True)  ✓
          │     → _get_col(target, field, alias, simple_col=True)
          │         → SimpleCol(target, field)  [无表名]
          └─ for Node children: _add_q(child, ...) # BUG: 未传 simple_col → 默认 False
                → build_filter(..., simple_col=False)
                      → _get_col(..., simple_col=False)
                          → Col(alias, target)  [带表名 "table"."col"]
```

F() 引用路径（`test_simplecol_query` 中的 `Q(num__lt=F('id'))`）：
```
build_filter(..., simple_col=True)
  → resolve_lookup_value(F('id'), ..., simple_col=True)
      → F.resolve_expression(query, simple_col=True)
          → query.resolve_ref('id', simple_col=True)
              → _get_col(..., simple_col=True) → SimpleCol
```

## 替换决策总览

本实例在 mutations.jsonl 中只有 Group B 一条记录，其余 A/C/D/E 为新设计。

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 新设计 | 新增 | mutations.jsonl 中不存在，按路径B新设计 |
| B | 🔴 必须替换 | 替换 | 原mutation将build_where的simple_col=False，效果等同完全禁用SimpleCol，不自然且过于宽泛失败 |
| C | 新设计 | 新增 | mutations.jsonl 中不存在，按路径B新设计 |
| D | 新设计 | 新增 | mutations.jsonl 中不存在，按路径B新设计 |
| E | 新设计 | 新增 | mutations.jsonl 中不存在，按路径B新设计 |

## 各组 Mutation 分析

### Group A — 替换（新设计）
**原 mutation**：不存在（路径 B）

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index d69c24419b..e557e4b310 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1050,7 +1050,7 @@ class Query(BaseExpression):
         if hasattr(value, 'resolve_expression'):
             kwargs = {'reuse': can_reuse, 'allow_joins': allow_joins}
             if isinstance(value, F):
-                kwargs['simple_col'] = simple_col
+                pass
             value = value.resolve_expression(self, **kwargs)
         elif isinstance(value, (list, tuple)):
             # The items of the iterable may be expressions and therefore need
```

**变异语义**：`resolve_lookup_value` 不再将 `simple_col` 传递给 F() 表达式的 `resolve_expression`。当 check constraint 中出现 `Q(num__lt=F('id'))` 这类使用 F() 引用的条件时，F() 会以默认的 `simple_col=False` 解析，生成带表名的 `Col` 而非 `SimpleCol`，导致 SQLite 上的 rename 后 CHECK constraint 失效。简单的非 F() 测试（`test_simple_query`、`test_complex_query`）会通过；只有使用 F() 引用的 `test_simplecol_query`（F2P测试）和 `test_multiple_fields` 会失败。这个 mutation 针对 F() 引用这一特定路径，比直接改 build_where 更精准、更难发现。

---

### Group B — 替换（原有 mutation 为必须替换）
**原 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index d69c24419b..173238730f 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1322,7 +1322,7 @@ class Query(BaseExpression):
         self.demote_joins(existing_inner)
 
     def build_where(self, q_object):
-        return self._add_q(q_object, used_aliases=set(), allow_joins=False, simple_col=True)[0]
+        return self._add_q(q_object, used_aliases=set(), allow_joins=False, simple_col=False)[0]
```

**分类**：🔴 必须替换
**理由**：将 `simple_col=True` 改为 `simple_col=False` 等于彻底禁用 SimpleCol，所有通过 `build_where` 生成的查询都不使用简单列名，不仅 F2P 失败，连 `test_foreign_key_exclusive`、`test_simple_query` 等基础测试也会失败（因为它们都断言 `isinstance(lhs, SimpleCol)`）。影响面过宽，不符合"通过大多数简单测试"的高质量标准。

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index d69c24419b..476c8bba65 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1338,7 +1338,8 @@ class Query(BaseExpression):
             if isinstance(child, Node):
                 child_clause, needed_inner = self._add_q(
                     child, used_aliases, branch_negated,
-                    current_negated, allow_joins, split_subq, simple_col)
+                    current_negated, allow_joins, split_subq,
+                    simple_col if connector == AND else False)
                 joinpromoter.add_votes(needed_inner)
             else:
                 child_clause, needed_inner = self.build_filter(
```

**变异语义**：`_add_q` 在递归处理子 Q 对象时，只在父连接符为 AND 时传播 `simple_col`，在 OR 时强制传 `False`。这直接模拟了原始 bug 的核心场景：OR 连接的子 Q 不使用 `SimpleCol`，导致 `Q(a=1) | Q(b=2)` 中的 OR 分支使用带表名的 `Col`。`test_simplecol_query`（F2P）断言 `where.children[0]` 和 `where.children[1]` 都是 `SimpleCol`，其中 OR 结构下的子节点会用 `Col` 而非 `SimpleCol`，导致断言失败。但针对纯 AND 的查询（如 `test_simple_query`、`test_transform`）不受影响，会继续通过。这是对原始 bug 根因的精确复现，但表达方式更隐晦（"只在 OR 时禁用"而非"从未传播"）。

---

### Group C — 新设计
**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index d69c24419b..eb86144c0b 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1639,7 +1639,7 @@ class Query(BaseExpression):
             join_info.transform_function(targets[0], final_alias)
             if reuse is not None:
                 reuse.update(join_list)
-            col = _get_col(targets[0], join_info.targets[0], join_list[-1], simple_col)
+            col = _get_col(targets[0], join_info.targets[0], join_list[-1], False)
             return col
 
     def split_exclude(self, filter_expr, can_reuse, names_with_path):
```

**变异语义**：`resolve_ref` 方法在最终生成列引用时忽略 `simple_col` 参数，始终使用 `Col`（带表名）。此方法是 F() 表达式解析的终点，F() 在 check constraint 中总会经过此路径。这使所有通过 F() 引用字段的 check constraint 条件都产生带表名的 SQL，复现 SQLite rename 失败场景。不涉及 F() 的普通查询（`test_simple_query`、`test_complex_query`、`test_foreign_key_exclusive`）完全不受影响；只有 `test_multiple_fields` 和 `test_simplecol_query` 中的 F() 相关断言会失败。`resolve_ref` 位于 `resolve_lookup_value` 的下游，修改位置与 golden patch 相差较远，不易关联。

---

### Group D — 新设计
**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index d69c24419b..caaff8009a 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1322,7 +1322,9 @@ class Query(BaseExpression):
         self.demote_joins(existing_inner)
 
     def build_where(self, q_object):
-        return self._add_q(q_object, used_aliases=set(), allow_joins=False, simple_col=True)[0]
+        from django.utils.tree import Node
+        has_nested = any(isinstance(c, Node) for c in q_object.children)
+        return self._add_q(q_object, used_aliases=set(), allow_joins=False, simple_col=not has_nested)[0]
```

**变异语义**：`build_where` 检测 Q 对象是否包含嵌套子 Q（`Node` 类型的 children）。若有，则以 `simple_col=False` 调用 `_add_q`，禁用 SimpleCol；若无，则正常用 `True`。这模拟了开发者"发现顶层嵌套 Q 用 SimpleCol 会有问题"这一错误认知，误以为只有平坦结构才需要 SimpleCol。实际上，含嵌套 Q 的 check constraint 恰恰最需要 SimpleCol（这正是 bug 的触发场景）。`test_simple_query`、`test_multiple_fields`、`test_transform`、`test_negated_nullable`、`test_foreign_key_exclusive` 都使用平坦或简单 Q，会通过；`test_complex_query`、`test_simplecol_query`（F2P，含嵌套 OR/AND 结构）会由于 `simple_col=False` 导致断言失败。代码看上去像是"优化"，逻辑貌似合理但实为错误方向。

---

### Group E — 新设计
**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index d69c24419b..55893b489c 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1338,7 +1338,8 @@ class Query(BaseExpression):
             if isinstance(child, Node):
                 child_clause, needed_inner = self._add_q(
                     child, used_aliases, branch_negated,
-                    current_negated, allow_joins, split_subq, simple_col)
+                    current_negated, allow_joins, split_subq,
+                    simple_col and not branch_negated)
                 joinpromoter.add_votes(needed_inner)
             else:
                 child_clause, needed_inner = self.build_filter(
```

**变异语义**：在 `_add_q` 递归时，`simple_col` 仅在非 branch_negated（即未经过任何否定）时传播。一旦进入带有 `~Q(...)` 否定的分支，所有后续子节点都会以 `simple_col=False` 处理，产生带表名的 `Col`。这模拟了"否定条件下需要完全限定名以区分歧义"这一错误的技术直觉。纯 AND/OR 无否定的查询（`test_complex_query`、`test_simplecol_query` 的非否定部分）会正常通过；而含 `~Q(...)` 的 `test_negated_nullable`（断言 `lookup.lhs.target` 而非 `SimpleCol`，可能仍通过）与 check constraint 中含否定 Q 的场景（F2P test_add_or_constraint 的实际约束行为）会失败。此 mutation 与 golden patch 修改的同一函数同一路径，但条件不同，是对递归传播语义的精确干扰。

## 新设计 Mutation 说明

**Group A（resolve_lookup_value）**：
基于调用链分析发现，F() 表达式的 `simple_col` 传播有独立路径：`build_filter` → `resolve_lookup_value` → `F.resolve_expression` → `query.resolve_ref`。在 `resolve_lookup_value` 中移除 `kwargs['simple_col'] = simple_col` 这行，切断了 F() 引用的 SimpleCol 传播。真实开发者可能认为"F() 是值表达式，不需要 SimpleCol 处理"。

**Group B（_add_q OR branch）**：
精确复现原始 bug 的本质：OR 分支丢失 `simple_col`。通过 `connector == AND` 判断，只在 AND 时传播，在 OR 时截断，是对原 bug 的精确模拟但以更显式的条件判断形式出现。

**Group C（resolve_ref）**：
F() 解析的最终端点，hardcode `False` 使所有 F() 引用生成 `Col`。此位置在调用栈深处，与 `build_where` 距离远，难以直接关联。

**Group D（build_where 条件检查）**：
在入口处根据 Q 结构动态决定 `simple_col`，逻辑看似优化（"平坦结构才用 SimpleCol"），实为反转正确语义。代码可读性好，不易被审查发现。

**Group E（branch_negated 条件）**：
在传播路径上加入 `not branch_negated` 条件，针对含否定的 Q 结构截断 `simple_col`。这是对 negation 处理逻辑的语义错误，模拟了混淆 `branch_negated` 与 `simple_col` 含义的开发者错误。
