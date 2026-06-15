# django__django-12774

## 问题背景

`QuerySet.in_bulk(field_name=...)` 要求目标字段必须是唯一字段，但旧代码只检查 `field.unique=True`，不识别通过 `UniqueConstraint` 声明的唯一约束。因此，当字段通过 `Meta.constraints` 中的 `UniqueConstraint` 保证唯一性时，`in_bulk()` 错误地抛出 `ValueError`。

Golden patch 在 `in_bulk` 中额外构建了 `unique_fields` 列表（从 `opts.total_unique_constraints` 中提取单字段、无条件的约束字段名），并将其作为第三个条件追加到原有的 `not field.unique` 检查之后。

## Golden Patch 语义分析

原来的检查：
```python
if field_name != 'pk' and not self.model._meta.get_field(field_name).unique:
    raise ValueError(...)
```

修复后：
```python
opts = self.model._meta
unique_fields = [
    constraint.fields[0]
    for constraint in opts.total_unique_constraints
    if len(constraint.fields) == 1
]
if (
    field_name != 'pk' and
    not opts.get_field(field_name).unique and
    field_name not in unique_fields
):
    raise ValueError(...)
```

核心逻辑：
1. `total_unique_constraints`（定义在 `options.py`）返回所有无条件（`condition is None`）的 `UniqueConstraint`，即能保证全行唯一性的约束。
2. 仅取 `len(constraint.fields) == 1` 的单字段约束，因为多字段约束不能保证单个字段的唯一性。
3. 在验证时，若字段名在 `unique_fields` 中，则绕过 ValueError，允许 `in_bulk` 继续执行。

两个关键过滤器的语义：
- `condition is None`：只允许全量约束（非 partial index），因为带条件的约束不保证全行唯一
- `len(constraint.fields) == 1`：只允许单字段约束，多字段联合约束不保证单个字段的唯一性

## 调用链分析

```
QuerySet.in_bulk(field_name)
  └── self.model._meta                          # Options 实例
  └── opts.total_unique_constraints             # options.py cached_property，过滤无条件 UniqueConstraint
  └── opts.get_field(field_name).unique         # 字段级 unique=True 检查
  └── self.filter(**{filter_key: id_list})      # 执行查询，field_name 为 filter key
```

`total_unique_constraints` 定义在 `django/db/models/options.py`，是 `Options` 类的 `@cached_property`：
```python
return [
    constraint
    for constraint in self.constraints
    if isinstance(constraint, UniqueConstraint) and constraint.condition is None
]
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 与 Group E 完全相同的 diff，直接冗余 |
| B | 🟡 语义浅层 | 保留 | `and` → `or` 改变布尔逻辑，修改位置在条件核心处，能模拟开发者逻辑运算符混淆 |
| C | （缺失）| 新设计 | 无原 mutation，设计全新高质量 mutation |
| D | 🟢 保留 | 保留 | 移除 `field_name not in unique_fields` 子句，等价于回退到补丁前行为，隐蔽性强 |
| E | 🟢 保留 | 保留 | `not in` → `in` 直接反转，UniqueConstraint 字段被拒绝而非允许 |

语义浅层共 1 个（B），无需替换（floor(1/2)=0）。

替换总数：A（冗余）1 个 + C（缺失）1 个 = 共新设计 2 个。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：
```diff
diff --git a/django/db/models/query.py b/django/db/models/query.py
index c1aa352701..0b35ec2c43 100644
--- a/django/db/models/query.py
+++ b/django/db/models/query.py
@@ -698,7 +698,7 @@ class QuerySet:
         if (
             field_name != 'pk' and
             not opts.get_field(field_name).unique and
-            field_name not in unique_fields
+            field_name in unique_fields
         ):
             raise ValueError("in_bulk()'s field_name must be a unique field but %r isn't." % field_name)
```
**分类**：🔴 必须替换
**理由**：与 Group E 的 diff 完全相同（同一文件同一行同一改动），直接冗余，必须替换。
**最终 mutation**（替换为新设计，跨文件变异到 `options.py`）：
```diff
diff --git a/django/db/models/options.py b/django/db/models/options.py
index 0e28b6812a..4fd91501a5 100644
--- a/django/db/models/options.py
+++ b/django/db/models/options.py
@@ -837,7 +837,7 @@ class Options:
         return [
             constraint
             for constraint in self.constraints
-            if isinstance(constraint, UniqueConstraint) and constraint.condition is None
+            if isinstance(constraint, UniqueConstraint) and constraint.condition is not None
         ]
```
**变异语义**：`total_unique_constraints` 的过滤条件从 `condition is None`（无条件 = 全量约束）改为 `condition is not None`（有条件 = 部分约束）。结果是：真正的全量 UniqueConstraint（如 `UniqueConstraint(fields=['year'])`）不再被识别为 total unique，`in_bulk(field_name='year')` 仍然抛出 ValueError。而带条件的部分约束（如 `UniqueConstraint(fields=['ean'], condition=Q(is_active=True))`）反而被当作全量约束，允许 `in_bulk` 执行，但结果可能包含重复键（因为部分约束不保证全行唯一）。
跨文件变异（`options.py` 而非 `query.py`），从调用链上游引入错误，难以在 `in_bulk` 的代码审查中发现。

---

### Group B — 保留
**原 mutation**：
```diff
diff --git a/django/db/models/query.py b/django/db/models/query.py
index c1aa352701..005da0dfd5 100644
--- a/django/db/models/query.py
+++ b/django/db/models/query.py
@@ -696,8 +696,8 @@ class QuerySet:
             if len(constraint.fields) == 1
         ]
         if (
-            field_name != 'pk' and
-            not opts.get_field(field_name).unique and
+            field_name != 'pk' or
+            not opts.get_field(field_name).unique or
             field_name not in unique_fields
         ):
             raise ValueError("in_bulk()'s field_name must be a unique field but %r isn't." % field_name)
```
**分类**：🟡 语义浅层（保留）
**理由**：`and` → `or` 是逻辑运算符替换，但改动在三个条件的连接方式上，使整个验证逻辑从"全部条件满足才报错"变为"任意条件满足即报错"。修改位置是核心条件判断节点，不孤立。能模拟开发者混淆 `and`/`or` 逻辑的真实错误。虽然只有两处修改，但影响较大且需要深入理解布尔逻辑才能发现。
**最终 mutation**（与原相同）：
```diff
diff --git a/django/db/models/query.py b/django/db/models/query.py
index c1aa352701..005da0dfd5 100644
--- a/django/db/models/query.py
+++ b/django/db/models/query.py
@@ -696,8 +696,8 @@ class QuerySet:
             if len(constraint.fields) == 1
         ]
         if (
-            field_name != 'pk' and
-            not opts.get_field(field_name).unique and
+            field_name != 'pk' or
+            not opts.get_field(field_name).unique or
             field_name not in unique_fields
         ):
             raise ValueError("in_bulk()'s field_name must be a unique field but %r isn't." % field_name)
```
**变异语义**：将 `A and B and C` 改为 `A or B or C`（第一、二条件改为 or，第三条件的 and 在原 diff 中未改动，但已被前两个 or 短路）。实际语义：只要 `field_name != 'pk'`，就会进入错误路径（因为 `pk` 之外的所有字段都满足第一条件）。所有非 pk 的 `in_bulk` 调用都会抛出 ValueError，包括 `unique=True` 的字段。`in_bulk(field_name='pk')`（默认）仍然正常工作（因为 `'pk' != 'pk'` 为 False，短路后整体 False）。

---

### Group C — 新设计
**原 mutation**：（缺失，全新设计）
**分类**：新设计
**最终 mutation**：
```diff
diff --git a/django/db/models/query.py b/django/db/models/query.py
index c1aa352701..06802eb531 100644
--- a/django/db/models/query.py
+++ b/django/db/models/query.py
@@ -693,7 +693,7 @@ class QuerySet:
         unique_fields = [
             constraint.fields[0]
             for constraint in opts.total_unique_constraints
-            if len(constraint.fields) == 1
+            if len(constraint.fields) >= 1
         ]
         if (
             field_name != 'pk' and
```
**变异语义**：将单字段约束过滤器从 `== 1` 改为 `>= 1`，使多字段联合约束（如 `UniqueConstraint(fields=['brand', 'name'])`）的第一个字段（`brand`）也被加入 `unique_fields`。结果是：`in_bulk(field_name='brand')` 通过验证但 `brand` 字段本身并不唯一（只有 brand+name 组合才唯一），查询结果字典中可能出现键被覆盖（只保留最后一个 brand 相同的对象）或结果不完整等错误，且没有错误提示。对于仅有单字段约束的场景（测试的正常路径）完全无影响；只有在模型存在多字段联合约束且用户误用时才暴露。

---

### Group D — 保留
**原 mutation**：
```diff
diff --git a/django/db/models/query.py b/django/db/models/query.py
index c1aa352701..ce846689f7 100644
--- a/django/db/models/query.py
+++ b/django/db/models/query.py
@@ -697,8 +697,7 @@ class QuerySet:
         ]
         if (
             field_name != 'pk' and
-            not opts.get_field(field_name).unique and
-            field_name not in unique_fields
+            not opts.get_field(field_name).unique
         ):
             raise ValueError("in_bulk()'s field_name must be a unique field but %r isn't." % field_name)
```
**分类**：🟢 保留
**理由**：移除 `field_name not in unique_fields` 条件，使新增的 `unique_fields` 检查完全失效，等价于回退到补丁前的行为（只检查 `field.unique`）。修改看起来像"代码简化"或"移除了多余的检查"，难以被快速代码审查发现。不在同一行做符号替换，而是删除了整个逻辑分支。
**最终 mutation**（与原相同）：
```diff
diff --git a/django/db/models/query.py b/django/db/models/query.py
index c1aa352701..ce846689f7 100644
--- a/django/db/models/query.py
+++ b/django/db/models/query.py
@@ -697,8 +697,7 @@ class QuerySet:
         ]
         if (
             field_name != 'pk' and
-            not opts.get_field(field_name).unique and
-            field_name not in unique_fields
+            not opts.get_field(field_name).unique
         ):
             raise ValueError("in_bulk()'s field_name must be a unique field but %r isn't." % field_name)
```
**变异语义**：`unique_fields` 列表仍然被构建，但不再参与条件判断。`in_bulk(field_name='year')` 对于只有 `UniqueConstraint(fields=['year'])` 的字段仍然抛出 ValueError，因为 `year.unique` 为 False。所有通过 `unique=True` 唯一的字段正常工作；仅 UniqueConstraint 方式唯一的字段被拒绝。

---

### Group E — 保留
**原 mutation**：
```diff
diff --git a/django/db/models/query.py b/django/db/models/query.py
index c1aa352701..0b35ec2c43 100644
--- a/django/db/models/query.py
+++ b/django/db/models/query.py
@@ -698,7 +698,7 @@ class QuerySet:
         if (
             field_name != 'pk' and
             not opts.get_field(field_name).unique and
-            field_name not in unique_fields
+            field_name in unique_fields
         ):
             raise ValueError("in_bulk()'s field_name must be a unique field but %r isn't." % field_name)
```
**分类**：🟢 保留
**理由**：修改在 golden fix 的关键条件处，`not in` → `in` 直接反转过滤语义：只有当字段名在 `unique_fields` 中时才抛 ValueError（即专门针对 UniqueConstraint 字段报错）。而没有 UniqueConstraint 的非 unique 字段反而通过了验证（可能导致 in_bulk 产生错误结果）。这是关键节点上的语义反转，能通过所有不涉及 UniqueConstraint 的测试，只在新增测试场景下失败。
**最终 mutation**（与原相同）：
```diff
diff --git a/django/db/models/query.py b/django/db/models/query.py
index c1aa352701..0b35ec2c43 100644
--- a/django/db/models/query.py
+++ b/django/db/models/query.py
@@ -698,7 +698,7 @@ class QuerySet:
         if (
             field_name != 'pk' and
             not opts.get_field(field_name).unique and
-            field_name not in unique_fields
+            field_name in unique_fields
         ):
             raise ValueError("in_bulk()'s field_name must be a unique field but %r isn't." % field_name)
```
**变异语义**：`unique_fields` 中的字段（有总量 UniqueConstraint 的字段）引发 ValueError；不在 `unique_fields` 中的非 unique 字段则错误地通过验证进入查询。两种场景下均产生错误：有 UniqueConstraint 的字段被拒绝（新增 F2P 测试失败），无 unique 约束的字段被错误放行（可能导致 P2P 测试的数据正确性问题）。

## 新设计 Mutation 说明

### Group A 新设计依据
`total_unique_constraints` 是 `options.py` 中的 `@cached_property`，定义了何为"全量唯一约束"。golden patch 依赖它来构建 `unique_fields`，因此在这个上游属性引入错误比在 `query.py` 中修改更难发现——代码审查只看 `in_bulk` 函数时完全看不出问题。将 `condition is None` 改为 `condition is not None` 正好反转了"全量"与"部分"约束的语义，模拟了开发者在阅读 Django 文档时对 `condition` 参数含义的误解（误以为有条件=全量）。这种跨文件变异在 `query.py` 的代码审查中完全透明。

### Group C 新设计依据
`len(constraint.fields) == 1` 是保证"单字段约束才能用于 in_bulk"的关键过滤器。改为 `>= 1` 允许多字段联合约束的第一个字段也加入 `unique_fields`，这模拟了开发者"只要约束包含该字段就应该允许"的直觉错误。与 A/E/D 相比，C 修改的是约束字段数量的判断逻辑，与其他 mutation 不重叠，且错误以静默方式体现（不是抛异常而是返回错误数据），更难被简单测试捕获。
