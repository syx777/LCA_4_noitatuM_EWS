# django__django-13569

## 问题背景

当 QuerySet 同时使用聚合（如 `Count`）和随机排序（`order_by('?')`）时，Django 会错误地将 `RANDOM()` 函数加入 SQL 的 `GROUP BY` 子句。例如：

```python
Author.objects.annotate(contact_count=Count('book')).order_by('?')
```

生成的 SQL 大致为：`SELECT ... FROM ... GROUP BY author.id, RANDOM()`，导致聚合分组失效——每行都有独立的随机值，使得 `GROUP BY` 按每条记录分组，`COUNT` 返回1而不是正确的聚合值。

根本原因：`RANDOM()` 是一个无参数的 `Func` 子类。`Expression.get_group_by_cols()` 默认对非聚合表达式返回 `[self]`（将自身加入 GROUP BY）。`Random` 类没有重写此方法，导致 `RANDOM()` 被错误纳入 GROUP BY。

## Golden Patch 语义分析

```python
# 修复：在 Random 类中添加 get_group_by_cols 重写
def get_group_by_cols(self, alias=None):
    return []
```

核心设计决策：
1. **返回空列表**：表示 `RANDOM()` 不应出现在 GROUP BY 子句中。`RANDOM()` 是非确定性函数，每次调用产生不同值，不具有分组意义。
2. **仅3行简洁修复**：通过覆盖基类行为，精准阻断 `Random` 参与 GROUP BY。
3. **不影响 ORDER BY**：`order_by('?')` 仍能正常工作，`RANDOM()` 仍出现在 `ORDER BY` 子句中，只是不进入 `GROUP BY`。

## 调用链分析

```
QuerySet.order_by('?')
    └─ 生成 ORDER BY RANDOM()
    
SQLCompiler.get_group_by()
    ├─ 遍历 SELECT 中的表达式 → 调用 expr.get_group_by_cols()
    └─ 遍历 ORDER BY 中的表达式 → 调用 expr.get_group_by_cols()
            └─ Random.get_group_by_cols()  [← golden patch 修改此处]
                    before fix: inherited from Expression → returns [self] → RANDOM() in GROUP BY
                    after fix:  returns []  → RANDOM() NOT in GROUP BY

Expression.get_group_by_cols(alias=None):
    if not self.contains_aggregate:
        return [self]   ← Random.contains_aggregate = False，所以默认返回 [self]
    cols = []
    for source in self.get_source_expressions():
        cols.extend(source.get_group_by_cols())
    return cols

# 另一调用路径：query.py 中 set_group_by() 对 annotation 调用时会传入 alias
annotation.get_group_by_cols(alias=alias)
```

数据流关键点：
- `Random.arity = 0` → 无 source expressions → `get_source_expressions()` 返回 `[]`
- `Random.contains_aggregate = False`（继承自 Expression）
- ORDER BY 路径中调用 `get_group_by_cols()` **不传 alias**
- annotation 路径中调用 `get_group_by_cols(alias=some_string)` **会传 alias**

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 新设计 | 设计新 mutation | mutations.jsonl 中无此实例，全新设计5个 |
| B | 新设计 | 设计新 mutation | 同上 |
| C | 新设计 | 设计新 mutation | 同上 |
| D | 新设计 | 设计新 mutation | 同上 |
| E | 新设计 | 设计新 mutation | 同上 |

路径 B（新实例），全部5组均为全新设计。

## 各组 Mutation 分析

### Group A — 新设计
**分类**：新设计（A1 类型：OOP 委托错误——调用父类本应被覆盖的行为）
**设计思路**：将 `return []` 改为 `return super().get_group_by_cols(alias)`，委托给基类 `Expression`。`Expression.get_group_by_cols()` 对于 `contains_aggregate=False` 的表达式（Random 就是）返回 `[self]`，导致 `RANDOM()` 重新被纳入 GROUP BY。看起来是"规范的 super() 调用"，实际破坏了修复。
**最终 mutation**：
```diff
diff --git a/django/db/models/functions/math.py b/django/db/models/functions/math.py
index 15915f4b7c..b0413e7c95 100644
--- a/django/db/models/functions/math.py
+++ b/django/db/models/functions/math.py
@@ -155,7 +155,7 @@ class Random(NumericOutputFieldMixin, Func):
         return super().as_sql(compiler, connection, function='RAND', **extra_context)
 
     def get_group_by_cols(self, alias=None):
-        return []
+        return super().get_group_by_cols(alias)
 
 
 class Round(Transform):
```
**变异语义**：`super().get_group_by_cols(alias)` 调用 `Expression` 的实现，由于 `Random.contains_aggregate=False`，直接返回 `[self]`，RANDOM() 进入 GROUP BY，聚合分组失效。`test_aggregation_random_ordering` 会返回每位作者9条记录（每行都被单独分组）而不是1条聚合记录，断言失败。

---

### Group B — 新设计
**分类**：新设计（B2 类型：误用 source_expressions 作为判断条件）
**设计思路**：`return [self] if not self.get_source_expressions() else []`。逻辑含义：若函数没有参数（source expressions 为空），则包含在 GROUP BY 中。开发者可能认为"无参数函数相当于常量，应该分组"，但 `RANDOM()` 是非确定性函数，不能当作常量。`Random.arity=0`，source_expressions 永为空 → 条件成立 → 返回 `[self]` → 破坏聚合。
**最终 mutation**：
```diff
diff --git a/django/db/models/functions/math.py b/django/db/models/functions/math.py
index 15915f4b7c..9060ff2944 100644
--- a/django/db/models/functions/math.py
+++ b/django/db/models/functions/math.py
@@ -155,7 +155,7 @@ class Random(NumericOutputFieldMixin, Func):
         return super().as_sql(compiler, connection, function='RAND', **extra_context)
 
     def get_group_by_cols(self, alias=None):
-        return []
+        return [self] if not self.get_source_expressions() else []
 
 
 class Round(Transform):
```
**变异语义**：无参数函数（source_expressions 为空）被认为是"常量"而纳入 GROUP BY。但 RANDOM() 每次调用返回不同值，分组逻辑失效。F2P 测试失败原因同 A 组。

---

### Group C — 新设计
**分类**：新设计（C1 类型：直接返回 [self]，最简单的破坏形式）
**设计思路**：直接将方法体改为 `return [self]`，无论何种上下文都将 Random 加入 GROUP BY。这是最直接的变异，模拟开发者对 get_group_by_cols 约定的误解，认为函数应该参与分组。
**最终 mutation**：
```diff
diff --git a/django/db/models/functions/math.py b/django/db/models/functions/math.py
index 15915f4b7c..793f7e5814 100644
--- a/django/db/models/functions/math.py
+++ b/django/db/models/functions/math.py
@@ -155,7 +155,7 @@ class Random(NumericOutputFieldMixin, Func):
         return super().as_sql(compiler, connection, function='RAND', **extra_context)
 
     def get_group_by_cols(self, alias=None):
-        return []
+        return [self]
 
 
 class Round(Transform):
```
**变异语义**：Random 始终参与 GROUP BY，等同于没有应用修复。F2P 测试 `test_aggregation_random_ordering` 失败。虽然直接，但与 super() 版本（A）不同，不依赖基类逻辑。

---

### Group D — 新设计
**分类**：新设计（D2 类型：alias 参数条件判断错误——混淆调用路径语义）
**设计思路**：`return [] if alias else [self]`。在 annotation 路径下（有 alias），返回 `[]`；在 ORDER BY 路径下（无 alias，alias=None），返回 `[self]`。逻辑看似"聪明"：有别名时说明已被 Ref 引用，无需重复加入 GROUP BY；无别名时需要显式加入。但这正颠倒了需求：ORDER BY RANDOM() 的路径（无 alias）恰恰是不应该加入 GROUP BY 的场景。
**最终 mutation**：
```diff
diff --git a/django/db/models/functions/math.py b/django/db/models/functions/math.py
index 15915f4b7c..0ed781f1ba 100644
--- a/django/db/models/functions/math.py
+++ b/django/db/models/functions/math.py
@@ -155,7 +155,7 @@ class Random(NumericOutputFieldMixin, Func):
         return super().as_sql(compiler, connection, function='RAND', **extra_context)
 
     def get_group_by_cols(self, alias=None):
-        return []
+        return [] if alias else [self]
 
 
 class Round(Transform):
```
**变异语义**：ORDER BY 路径调用 `get_group_by_cols()` 时 alias=None → else 分支 → `[self]` → RANDOM() 进入 GROUP BY。annotation 路径会正确返回 `[]`，但 F2P 测试走的是 ORDER BY 路径，故测试失败。此 mutation 最难被发现：它在 annotation 场景下行为正确，只在 ORDER BY RANDOM() 场景下失败。

---

### Group E — 新设计
**分类**：新设计（E2 类型：contains_aggregate 属性判断——混淆聚合与 GROUP BY 排除的概念）
**设计思路**：
```python
def get_group_by_cols(self, alias=None):
    if self.contains_aggregate:
        return []
    return [self]
```
开发者可能认为："只有聚合函数不能在 GROUP BY 里（因为聚合函数本身定义了分组），非聚合函数都应该参与 GROUP BY。" 但 `Random.contains_aggregate = False`（它不是聚合函数），所以走 `return [self]` 分支，RANDOM() 进入 GROUP BY，破坏聚合。
**最终 mutation**：
```diff
diff --git a/django/db/models/functions/math.py b/django/db/models/functions/math.py
index 15915f4b7c..d9743fdc18 100644
--- a/django/db/models/functions/math.py
+++ b/django/db/models/functions/math.py
@@ -155,7 +155,9 @@ class Random(NumericOutputFieldMixin, Func):
         return super().as_sql(compiler, connection, function='RAND', **extra_context)
 
     def get_group_by_cols(self, alias=None):
-        return []
+        if self.contains_aggregate:
+            return []
+        return [self]
 
 
 class Round(Transform):
```
**变异语义**：`Random.contains_aggregate = False` → 跳过 `return []` → `return [self]` → RANDOM() 进入 GROUP BY。此 mutation 模拟对 "为什么要返回 []" 的错误理解：开发者以为只有聚合函数才需要被排除出 GROUP BY，而实际上非确定性函数（如 RANDOM()）同样需要被排除。

## 新设计 Mutation 说明

### 整体设计思路

本实例的 golden patch 极其简洁（3行），位于一个专门用于控制 GROUP BY 行为的接口方法 `get_group_by_cols` 中。所有5个 mutation 均针对同一方法，但攻击的是不同层面的语义理解：

1. **A (super())**: 对 OOP 继承的误用——认为调用 super() 是更规范的实现
2. **B (source_expressions check)**: 对"无参数函数即常量"的错误假设
3. **C ([self] directly)**: 最简单的破坏，不理解返回 [] 的意义
4. **D (alias conditional)**: 对两种调用路径（ORDER BY vs annotation）的语义混淆
5. **E (contains_aggregate)**: 混淆聚合函数排除（aggregate）与非确定性函数排除（random）的概念

D 是最难检测的：它的代码看起来"更智能"（考虑了 alias 参数），在 annotation 使用场景下也能正确工作，只在 `order_by('?')` 加聚合的特定场景下失败。
