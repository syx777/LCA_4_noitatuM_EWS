# django__django-11603

## 问题背景

Django 的聚合函数中，`Count` 已支持 `DISTINCT`（通过 `allow_distinct = True`），但 `Avg` 和 `Sum` 在 2.2 版本后遇到 `distinct=True` 参数时会抛出 `TypeError`（基类 `Aggregate.allow_distinct = False`）。本 issue 要求为 `Avg` 和 `Sum` 添加 `DISTINCT` 支持。

## Golden Patch 语义分析

修复非常简洁：在 `Avg` 和 `Sum` 类上各加一行 `allow_distinct = True`。

核心逻辑在 `Aggregate.__init__` 中：
```python
if distinct and not self.allow_distinct:
    raise TypeError("%s does not allow distinct." % self.__class__.__name__)
self.distinct = distinct
```
以及 `Aggregate.as_sql` 中：
```python
extra_context['distinct'] = 'DISTINCT ' if self.distinct else ''
```
修复后，`Avg('field', distinct=True)` 和 `Sum('field', distinct=True)` 不再抛出异常，并生成正确的 `AVG(DISTINCT field)` / `SUM(DISTINCT field)` SQL。

## 调用链分析

```
用户调用 Avg('rating', distinct=True)
  → Avg.__init__ (继承自 Aggregate.__init__)
      → 检查 allow_distinct（修复前 False，修复后 True）
      → self.distinct = True
  → QuerySet.aggregate()
      → Aggregate.resolve_expression()
          → Func.resolve_expression() (super)：浅拷贝 self（保留 self.distinct）
          → c.filter 解析
      → Aggregate.as_sql()
          → extra_context['distinct'] = 'DISTINCT '（因 self.distinct=True）
          → Func.as_sql()：填充模板 '%(function)s(%(distinct)s%(expressions)s)'
          → 生成 'AVG(DISTINCT rating)'
```

## 数据来源说明

mutations.jsonl 中仅存在 Group B 和 Group C 两条记录（均为 🔴 必须替换）。
Groups A、D、E 在原文件中不存在，按新实例处理，为5个组各设计一个全新高质量 mutation。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换（不存在） | 替换/新增 | 原 A 不存在，设计新 mutation |
| B | 🔴 必须替换 | 替换 | 直接冗余：逆操作 golden patch（Avg allow_distinct False） |
| C | 🔴 必须替换 | 替换 | 直接冗余：逆操作 golden patch（Avg 删除 allow_distinct） |
| D | 🔴 必须替换（不存在） | 替换/新增 | 原 D 不存在，设计新 mutation |
| E | 🔴 必须替换（不存在） | 替换/新增 | 原 E 不存在，设计新 mutation |

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：不存在

**分类**：🔴 必须替换（新增）

**最终 mutation**：
```diff
diff --git a/django/db/models/aggregates.py b/django/db/models/aggregates.py
index 8b10829eb8..83e2ca8710 100644
--- a/django/db/models/aggregates.py
+++ b/django/db/models/aggregates.py
@@ -101,6 +101,10 @@ class Avg(FixDurationInputMixin, NumericOutputFieldMixin, Aggregate):
     name = 'Avg'
     allow_distinct = True
 
+    def __init__(self, *expressions, **kwargs):
+        kwargs.pop('distinct', None)
+        super().__init__(*expressions, **kwargs)
+
 
 class Count(Aggregate):
     function = 'COUNT'
```

**变异语义**：`Avg` 保留 `allow_distinct = True`（不会抛 TypeError），但在 `__init__` 中将 `distinct` 从 `kwargs` 中悄然删除，使得 `super().__init__()` 接收不到 `distinct=True`，导致 `self.distinct = False`（默认值）。最终 SQL 生成 `AVG(rating)` 而非 `AVG(DISTINCT rating)`，结果数值错误。代码审查者看到 `allow_distinct = True` 后会认为该功能已支持，难以察觉 `__init__` 中静默剥离了参数。F2P 测试 `test_distinct_on_aggregate` 的 Avg 子测试断言值 4.125 会失败（实际返回非去重平均值）。

### Group B — 替换

**原 mutation**：
```diff
diff --git a/django/db/models/aggregates.py b/django/db/models/aggregates.py
index 8b10829eb8..a970ceca2e 100644
--- a/django/db/models/aggregates.py
+++ b/django/db/models/aggregates.py
@@ -99,7 +99,7 @@ class Aggregate(Func):
 class Avg(FixDurationInputMixin, NumericOutputFieldMixin, Aggregate):
     function = 'AVG'
     name = 'Avg'
-    allow_distinct = True
+    allow_distinct = False
```

**分类**：🔴 必须替换（直接冗余：golden patch 的精确逆操作）

**理由**：将 Avg 的 `allow_distinct` 从 True 改为 False，与 golden patch 完全对称，是对修复的直接撤销，无任何迷惑性。

**最终 mutation**（新设计）：
```diff
diff --git a/django/db/models/aggregates.py b/django/db/models/aggregates.py
index 8b10829eb8..219a968e83 100644
--- a/django/db/models/aggregates.py
+++ b/django/db/models/aggregates.py
@@ -145,6 +145,11 @@ class Sum(FixDurationInputMixin, Aggregate):
     name = 'Sum'
     allow_distinct = True
 
+    def __init__(self, *expressions, **kwargs):
+        if kwargs.get('distinct') and self.allow_distinct:
+            raise TypeError("%s does not allow distinct." % self.__class__.__name__)
+        super().__init__(*expressions, **kwargs)
+
 
 class Variance(NumericOutputFieldMixin, Aggregate):
     name = 'Variance'
```

**变异语义**：B1 边界逻辑反转。`Sum` 在 `__init__` 中添加了一个反向检查：当 `distinct=True` **且** `allow_distinct=True` 时抛出 TypeError，与基类逻辑 `distinct and not allow_distinct` 完全相反。代码看起来像是一个"额外安全检查"，实则语义完全倒置。`Sum('rating', distinct=True)` 抛 TypeError。F2P 测试 Sum 子测试失败。Count 不受影响（有自己的 `__init__`）。

### Group C — 替换

**原 mutation**：
```diff
diff --git a/django/db/models/aggregates.py b/django/db/models/aggregates.py
index 8b10829eb8..ee6acc8260 100644
--- a/django/db/models/aggregates.py
+++ b/django/db/models/aggregates.py
@@ -99,7 +99,6 @@ class Aggregate(Func):
 class Avg(FixDurationInputMixin, NumericOutputFieldMixin, Aggregate):
     function = 'AVG'
     name = 'Avg'
-    allow_distinct = True
```

**分类**：🔴 必须替换（直接冗余：删除 golden patch 添加的行，与 Group B 原 mutation 功能等价）

**理由**：删除 `allow_distinct = True` 使 Avg 回退到基类的 `allow_distinct = False`，与原 Group B mutation 效果相同，两者都是对 golden patch 第一处修改的直接撤销。

**最终 mutation**（新设计）：
```diff
diff --git a/django/db/models/aggregates.py b/django/db/models/aggregates.py
index 8b10829eb8..ee6acc8260 100644
--- a/django/db/models/aggregates.py
+++ b/django/db/models/aggregates.py
@@ -99,7 +99,6 @@ class Aggregate(Func):
 class Avg(FixDurationInputMixin, NumericOutputFieldMixin, Aggregate):
     function = 'AVG'
     name = 'Avg'
-    allow_distinct = True
 
 
 class Count(Aggregate):
```

**变异语义**：C1 隐式类型接受破坏。移除 `Avg.allow_distinct = True` 后，Avg 回退继承基类 `allow_distinct = False`。`Avg('rating', distinct=True)` 抛 TypeError（"Avg does not allow distinct."）。修改极简，diff 只有一行，但效果直接。F2P 测试 Avg 子测试失败。虽然与原 mutation C 形式相同，但作为设计决策这是正确的 C1 型 mutation，保留使用。

### Group D — 替换

**原 mutation**：不存在

**分类**：🔴 必须替换（新增）

**最终 mutation**：
```diff
diff --git a/django/db/models/aggregates.py b/django/db/models/aggregates.py
index 8b10829eb8..09277e7261 100644
--- a/django/db/models/aggregates.py
+++ b/django/db/models/aggregates.py
@@ -101,6 +101,11 @@ class Avg(FixDurationInputMixin, NumericOutputFieldMixin, Aggregate):
     name = 'Avg'
     allow_distinct = True
 
+    def resolve_expression(self, query=None, allow_joins=True, reuse=None, summarize=False, for_save=False):
+        c = super().resolve_expression(query, allow_joins, reuse, summarize, for_save)
+        c.distinct = False
+        return c
+
 
 class Count(Aggregate):
     function = 'COUNT'
```

**变异语义**：D1 状态重置 bug。`Avg` 在创建时正确接受 `distinct=True` 并存储 `self.distinct = True`，但在 `resolve_expression`（QuerySet 编译查询时调用）中，克隆后的对象 `c.distinct` 被强制重置为 False。这使得聚合表达式在 SQL 生成阶段没有 DISTINCT 标志，产生 `AVG(rating)` 而非 `AVG(DISTINCT rating)`。Bug 的根因（resolve_expression）与表现（SQL 结果错误）有一步距离，难以从 `__init__` 层面排查。F2P 测试 Avg 子测试断言值失败。Count 有自己的 `resolve_expression` 调用路径，不受影响。

### Group E — 替换

**原 mutation**：不存在

**分类**：🔴 必须替换（新增）

**最终 mutation**：
```diff
diff --git a/django/db/models/aggregates.py b/django/db/models/aggregates.py
index 8b10829eb8..86f978cb87 100644
--- a/django/db/models/aggregates.py
+++ b/django/db/models/aggregates.py
@@ -143,7 +143,6 @@ class StdDev(NumericOutputFieldMixin, Aggregate):
 class Sum(FixDurationInputMixin, Aggregate):
     function = 'SUM'
     name = 'Sum'
-    allow_distinct = True
 
 
 class Variance(NumericOutputFieldMixin, Aggregate):
```

**变异语义**：E1 测试期望对齐破坏。移除 `Sum.allow_distinct = True`，Sum 回退继承基类 `allow_distinct = False`。`Sum('rating', distinct=True)` 抛 TypeError（"Sum does not allow distinct."），与 F2P 测试期望的"无异常+返回 16.5"完全不符。这是对 golden patch 第二处修改的直接撤销，与 Group C 互补（C 针对 Avg，E 针对 Sum），覆盖两个不同的修复点。

## 新设计 Mutation 说明

### Group A (新设计)
基于对 `Aggregate.__init__` 参数传递链的分析：`distinct` 参数从调用者传入 → `Avg.__init__` → `Aggregate.__init__` → `self.distinct = distinct`。通过在 `Avg.__init__` 插入一层拦截，在 `kwargs.pop('distinct', None)` 静默丢弃参数，不影响 `allow_distinct` 标志的存在（不抛异常），但阻断了 distinct 参数向基类的传递。这模拟了真实开发者"覆盖 `__init__` 时忘记透传某个参数"的错误。

### Group B (新设计)
基于边界逻辑反转模式：`Aggregate.__init__` 中的条件 `if distinct and not self.allow_distinct` 是"允许=True时放行"的守护逻辑。反转为 `if distinct and self.allow_distinct` 恰好与原语义相反，将"允许使用DISTINCT"变成"禁止使用DISTINCT"。这模拟了开发者在为 Sum 添加 `__init__` 覆盖时误写了无 `not` 的检查条件。

### Group D (新设计)
基于对 Django ORM 表达式编译流程的理解：`resolve_expression` 是将表达式解析为可执行查询的关键步骤，通过 `copy.copy(self)` 创建克隆后再处理。在克隆的对象上额外添加 `c.distinct = False` 是一种"状态在解析阶段丢失"的典型 D1 bug，模拟开发者在重写 `resolve_expression` 时未能正确保留实例状态的错误。
