# django__django-12663

## 问题背景

Django 在 commit 35431298 中将 `Query.output_field` 属性中的 `getattr(select, 'target', None) or select.field` 改为了 `self.select[0].field`，引入了回归：使用 `SimpleLazyObject` 作为嵌套子查询注解的过滤值时报错。

根本原因：当 `Subquery` 中选择了一个外键列（`Col` 对象）时，`Query.output_field` 应返回 `Col.target`（即 FK 字段本身，如 `ForeignKey`），而不是 `Col.field`（即 `output_field`，默认与 `target` 相同）。FK 字段有 `target_field` 属性（指向被引用的 PK 字段），`FieldGetDbPrepValueMixin.get_db_prep_lookup` 通过 `lhs.output_field.target_field` 获取正确的 `get_db_prep_value`。如果返回的是 `select.field`（FK 的 `output_field`，不一定有 `target_field`），则在处理 `SimpleLazyObject` 等延迟对象时无法正确转换值。

## Golden Patch 语义分析

修复核心：恢复了在 `Query.output_field` 中优先返回 `select.target` 的逻辑：

```python
select = self.select[0]
return getattr(select, 'target', None) or select.field
```

- `Col` 对象有 `target` 属性（指向模型字段，如 FK 字段）
- FK 字段有 `target_field` 属性（指向被引用的 PK 字段）
- 查找层级：`Col.target`（FK）→ `.target_field`（PK）→ `.get_db_prep_value()`
- 如果返回 `select.field`（Col 的 `output_field`），查找 `output_field.target_field` 可能失败或返回错误字段

## 调用链分析

```
query.filter(ceo_manager=max_manager)  [max_manager 是 SimpleLazyObject]
  └── Query.build_filter()
        └── Query.resolve_lookup_value(max_manager, ...)  → max_manager（不变，无 resolve_expression）
        └── Query.build_lookup(lookups, reffed_expression, max_manager)
              └── Lookup.get_db_prep_lookup(max_manager, connection)
                    └── FieldGetDbPrepValueMixin.get_db_prep_lookup()
                          └── field = getattr(self.lhs.output_field, 'target_field', None)
                                                                         ↑
                                                     lhs.output_field = Subquery.output_field
                                                                       = Subquery._resolve_output_field()
                                                                       = self.query.output_field
                                                                       = Query.output_field
                                                                       = select.target (FK field)
                                                                         → .target_field → PK field
                          └── get_db_prep_value(max_manager, ...)  [PK field 的 get_db_prep_value]
```

关键路径：`Query.output_field` → `Subquery._resolve_output_field` → `lhs.output_field` → `FieldGetDbPrepValueMixin.target_field` → 正确的值转换。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | 直接将 `'target'` 改为 `'source'`，等效于回退 golden patch（Col 无 source 属性） |
| B | 语义浅层 | 保留 | `or` → `and`，Col.target 存在时返回 `select.field` 而非 `select.target`，唯一语义浅层，floor(1/2)=0 |
| C | 新设计 | 替换（新建） | 原数据无 C 组 |
| D | 必须替换 | 替换 | `self.select[0].field` 两行版本，直接等价于回退 golden patch，与 A 完全重复 |
| E | 必须替换 | 替换 | `check_target_field=False` 死代码标志，人工痕迹极其明显 |

语义浅层共 1 个（B），floor(1/2)=0 个替换，保留 B。需替换 A、D、E，新建 C。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：将 `'target'` 保留但去掉了变量 `select`，直接返回 `select.field`。
**分类**：🔴 必须替换（直接还原 golden patch 逻辑）

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index e5524a8198..3ec4b48dd3 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -234,7 +234,7 @@ class Query(BaseExpression):
     def output_field(self):
         if len(self.select) == 1:
             select = self.select[0]
-            return getattr(select, 'target', None) or select.field
+            return getattr(select, 'source', None) or select.field
         elif len(self.annotation_select) == 1:
             return next(iter(self.annotation_select.values())).output_field
 
```
**变异语义**：`Col` 对象没有 `source` 属性，`getattr(select, 'source', None)` 始终返回 `None`，于是退回 `select.field`——等效于回退 golden patch，但通过改变属性名而非删除代码来实现，更难识别为"去除了 target 查找"。对所有将 FK 列选入子查询的场景均失败，SimpleLazyObject 过滤测试必然失败。

---

### Group B — 保留
**原 mutation**：
```diff
-            return getattr(select, 'target', None) or select.field
+            return getattr(select, 'target', None) and select.field
```
**分类**：🟡 语义浅层（保留）
**理由**：`or` → `and`。当 `select.target` 存在（truthy）时，`and` 返回最后一个真值 `select.field`，而非 `select.target`。等效于回退 golden patch 中对 FK 子查询的修复，但改变了逻辑运算符。是唯一的语义浅层，floor(1/2)=0，保留。
**最终 mutation**：与原相同。
**变异语义**：对于有 `target` 属性的 Col，返回 `select.field`（output_field）而非 `select.target`（FK 字段），使 `target_field` 查找失效，SimpleLazyObject 过滤失败。

---

### Group C — 替换（新设计）
**原 mutation**：无（原数据中不存在 C 组）

**最终 mutation**：
```diff
diff --git a/django/db/models/expressions.py b/django/db/models/expressions.py
index 8d37c457ce..eed2a0634c 100644
--- a/django/db/models/expressions.py
+++ b/django/db/models/expressions.py
@@ -1035,6 +1035,8 @@ class Subquery(Expression):
         self.query = exprs[0]
 
     def _resolve_output_field(self):
+        if len(self.query.select) == 1:
+            return self.query.select[0].field
         return self.query.output_field
 
     def copy(self):
```
**变异语义**：在 `Subquery._resolve_output_field` 中短路 `Query.output_field`，直接返回 `self.query.select[0].field`（Col 的 output_field），绕过了 golden patch 中 `Query.output_field` 对 `target` 的优先查找。改动位置在调用链上游（Subquery 层）而非 Query 层，且看起来像"优化"（直接访问比调用属性快）。典型 FK 子查询场景下，`select[0].field` 是 FK 字段的 output_field，通常与 target 相同，但缺少 `target_field` 属性链路，导致 SimpleLazyObject 转换失败。

---

### Group D — 替换
**原 mutation**：两行合并为一行 `return self.select[0].field`，与 Group A 语义完全相同。
**分类**：🔴 必须替换（直接回退 golden patch，与 A 完全重复）

**最终 mutation**：
```diff
diff --git a/django/db/models/lookups.py b/django/db/models/lookups.py
index d32c891284..91de2413a4 100644
--- a/django/db/models/lookups.py
+++ b/django/db/models/lookups.py
@@ -195,7 +195,7 @@ class FieldGetDbPrepValueMixin:
     def get_db_prep_lookup(self, value, connection):
         # For relational fields, use the 'target_field' attribute of the
         # output_field.
-        field = getattr(self.lhs.output_field, 'target_field', None)
+        field = getattr(self.lhs.output_field, 'remote_field', None)
         get_db_prep_value = getattr(field, 'get_db_prep_value', None) or self.lhs.output_field.get_db_prep_value
         return (
             '%s',
```
**变异语义**：跨文件修改 `FieldGetDbPrepValueMixin.get_db_prep_lookup`，将 `target_field` 改为 `remote_field`。FK 字段的 `remote_field` 是一个 `ManyToOneRel` 对象，不是 field，没有 `get_db_prep_value` 方法，所以 `getattr(field, 'get_db_prep_value', None)` 返回 `None`，退回到 `self.lhs.output_field.get_db_prep_value`（FK 字段自身）。FK 字段的 `get_db_prep_value` 内部调用 `self.target_field.get_db_prep_value`，在大多数情况下仍然正常；但对于 `SimpleLazyObject`，FK 字段的 `get_db_prep_value` 会调用 `self.target_field.get_db_prep_value(value, ...)` 而不先解包 lazy 对象，导致类型不匹配。改动在 `lookups.py` 而非 `query.py`，难以与 golden patch 的修复关联。

---

### Group E — 替换
**原 mutation**：添加 `check_target_field = False` 类属性和条件分支，使 `target` 路径永不执行。
**分类**：🔴 必须替换（人工痕迹极其明显：下划线前缀的内部标志、永远 False 的死代码）

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index e5524a8198..3357c095f7 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -234,7 +234,7 @@ class Query(BaseExpression):
     def output_field(self):
         if len(self.select) == 1:
             select = self.select[0]
-            return getattr(select, 'target', None) or select.field
+            return select.field or getattr(select, 'target', None)
         elif len(self.annotation_select) == 1:
             return next(iter(self.annotation_select.values())).output_field
```
**变异语义**：将 `or` 两侧操作数交换：`select.field or getattr(select, 'target', None)`。对于 `Col` 对象，`select.field` 是该列的 `output_field`，始终存在且为真值（是一个 Field 实例），所以 `or` 短路直接返回 `select.field`，永远不会去查 `select.target`。视觉上与原代码极为相似（只是交换了 `or` 的两个操作数），但语义完全等同于回退 golden patch 的修复。在代码审查中极易被忽视——两个表达式都引用了 `select` 的合法属性，逻辑运算符也相同。

## 新设计 Mutation 说明

### Group A（新）：属性名拼写变体
`'target'` → `'source'`。Col 对象的属性是 `target`（存储 target field），没有 `source`。这个改变通过"相近但错误的属性名"来规避检测，而非直接删除逻辑。对熟悉 Django ORM 内部结构的开发者来说 'source' 是个合理的猜测（参考 `Ref.source`），但在 Col 上不存在。

### Group C（新）：在调用链上游绕过修复
在 `Subquery._resolve_output_field` 中直接访问 `select[0].field`，绕过了 `Query.output_field` 中对 `target` 的优先查找。这是跨文件的调用链变异：golden patch 修复了 Query 层的 output_field 返回值，此 mutation 在 Subquery 层提前短路，让修复无效。代码看起来像"对单个 select 列的特殊优化路径"。

### Group D（新）：在值转换链末端引入错误
修改 `FieldGetDbPrepValueMixin.get_db_prep_lookup` 中的 `target_field` → `remote_field`。这是在值准备阶段（而非 output_field 解析阶段）引入错误，与 golden patch 的修改点（query.py）在不同文件，且错误只在特定值类型（SimpleLazyObject）下显现，通过所有常规对象实例的过滤测试。

### Group E（新）：操作数交换
`or` 两侧交换：`select.field or getattr(select, 'target', None)`。`select.field` 始终非 None（Field 实例），`or` 短路后 `target` 永远不被返回。视觉上与原代码几乎无法区分，只有理解 Python `or` 短路语义并知道 `select.field` 总是真值的人才能发现。
