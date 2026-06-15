# django__django-10880

## 问题背景

当 Django 的 `Count` 聚合函数同时使用 `Case/When` 条件表达式和 `distinct=True` 参数时，生成的 SQL 中 `DISTINCT` 关键字和表达式之间缺少空格，产生语法错误。例如：

```python
Count(Case(When(pages__gt=300, then='rating')), distinct=True)
```

在 base_commit 状态下生成的 SQL 为：

```sql
COUNT(DISTINCTCASE WHEN "book"."pages" > 300 THEN "book"."rating" END)
```

而正确应为：

```sql
COUNT(DISTINCT CASE WHEN "book"."pages" > 300 THEN "book"."rating" END)
```

## Golden Patch 语义分析

修复非常精确：在 `Aggregate.as_sql` 中，`extra_context['distinct']` 的值从 `'DISTINCT'` 改为 `'DISTINCT '`（尾部添加空格）。

修复核心逻辑：`Aggregate` 类的模板为 `'%(function)s(%(distinct)s%(expressions)s)'`，`%(distinct)s` 和 `%(expressions)s` 直接拼接，没有分隔符。因此当 `distinct=True` 时，`distinct` 字符串本身必须携带尾部空格，才能与后续的表达式 SQL 之间产生间隔。

原代码 `'DISTINCT'`（无空格）对于简单列标识符（如 `"rating"`）在 SQLite 中仍能工作，因为 SQLite 的词法分析器会把 `DISTINCT"rating"` 中的 `DISTINCT` 识别为关键字；但对于 `CASE` 这类以字母开头的关键字，`DISTINCTCASE` 会被当作一个不存在的标识符，导致语法错误。

## 调用链分析

```
ORM QuerySet.aggregate()
  → SQLCompiler.as_sql()
    → compiler.compile(aggregate_expr)
      → Aggregate.as_sql(compiler, connection, **extra_context)
          ↓ 设置 extra_context['distinct']
          ↓ 调用 super().as_sql() = Func.as_sql()
            → 用 template % data 拼接最终 SQL
              template = '%(function)s(%(distinct)s%(expressions)s)'
              data['distinct'] = 'DISTINCT ' (or '')
              data['expressions'] = compiled expression SQL
```

数据流：`self.distinct`（bool）→ `extra_context['distinct']`（str）→ `Func.as_sql` 中的 `data` dict → `template % data` → 最终 SQL 字符串。

`distinct` 字符串是唯一一个负责在 DISTINCT 关键字和表达式之间插入空格的地方。

## 替换决策总览

注意：原始 mutations.jsonl 中只有 B/C/D/E 四组，缺少 A 组，需要新建。

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 缺失 | 新建 | mutations.jsonl 中无 A 组，需新设计 |
| B | 🔴 必须替换 | 替换 | 逻辑取反（`not self.distinct`），与 golden patch 直接互逆 |
| C | 🔴 必须替换 | 替换 | 还原原始 bug（无空格的 `'DISTINCT'`），是 golden patch 的直接逆操作 |
| D | 🔴 必须替换 | 替换 | 含 `# BUG: Mark distinct as reset` 注释，极不自然，代码审查立即发现 |
| E | 🔴 必须替换 | 替换 | 引入 `distinct_spacing` 参数默认 `False`，效果等同于还原原始 bug，功能等价冗余 |

语义浅层共 0 个（全部为必须替换）。

## 各组 Mutation 分析

### Group A — 新建（全新设计）

**原 mutation**：无（mutations.jsonl 中缺少 A 组）

**分类**：🆕 新建

**最终 mutation**：
```diff
diff --git a/django/db/models/aggregates.py b/django/db/models/aggregates.py
index ea88c54b0d..393292e48d 100644
--- a/django/db/models/aggregates.py
+++ b/django/db/models/aggregates.py
@@ -67,8 +67,11 @@ class Aggregate(Func):
     def get_group_by_cols(self):
         return []
 
+    def _get_distinct_prefix(self):
+        return 'DISTINCT' if self.distinct else ''
+
     def as_sql(self, compiler, connection, **extra_context):
-        extra_context['distinct'] = 'DISTINCT ' if self.distinct else ''
+        extra_context['distinct'] = self._get_distinct_prefix()
         if self.filter:
             if connection.features.supports_aggregate_filter_clause:
                 filter_sql, filter_params = self.filter.as_sql(compiler, connection)
```

**变异语义**：将 `distinct` 字符串的生成逻辑提取到辅助方法 `_get_distinct_prefix()` 中，但该方法返回 `'DISTINCT'`（无尾部空格）。看起来像一次合理的"方法提取"重构，但遗漏了尾部空格。P2P 测试（`COUNT(DISTINCT"field")`）在 SQLite 中仍能工作；F2P 测试（`COUNT(DISTINCTCASE WHEN...)`）因 `DISTINCTCASE` 不是有效关键字而触发 SQL 语法错误。

---

### Group B — 替换

**原 mutation**：
```diff
-        extra_context['distinct'] = 'DISTINCT ' if self.distinct else ''
+        extra_context['distinct'] = 'DISTINCT ' if not self.distinct else ''
```
**分类**：🔴 必须替换
**理由**：将条件取反，当 `distinct=True` 时返回 `''`，当 `distinct=False` 时返回 `'DISTINCT '`。这是 golden patch 的直接逻辑逆操作，语义上完全相反，显而易见。

**最终 mutation**：
```diff
diff --git a/django/db/models/aggregates.py b/django/db/models/aggregates.py
index ea88c54b0d..154d068753 100644
--- a/django/db/models/aggregates.py
+++ b/django/db/models/aggregates.py
@@ -68,7 +68,7 @@ class Aggregate(Func):
         return []
 
     def as_sql(self, compiler, connection, **extra_context):
-        extra_context['distinct'] = 'DISTINCT ' if self.distinct else ''
+        extra_context['distinct'] = ('DISTINCT ' if self.distinct else '').rstrip()
         if self.filter:
             if connection.features.supports_aggregate_filter_clause:
                 filter_sql, filter_params = self.filter.as_sql(compiler, connection)
```

**变异语义**：在 `'DISTINCT '` 字符串上调用 `.rstrip()`，看起来像"去除字符串尾部多余空白"的防御性编码习惯，但实际上 `.rstrip()` 会移除结尾的空格，还原为 `'DISTINCT'`（无空格）。代码审查者可能认为这是无害的字符串规范化。

---

### Group C — 替换

**原 mutation**：
```diff
-        extra_context['distinct'] = 'DISTINCT ' if self.distinct else ''
+        extra_context['distinct'] = 'DISTINCT' if self.distinct else ''
```
**分类**：🔴 必须替换
**理由**：直接还原 golden patch 修复前的 bug 代码（`'DISTINCT'` 无空格），是 golden patch 最直接的逆操作。

**最终 mutation**：
```diff
diff --git a/django/db/models/aggregates.py b/django/db/models/aggregates.py
index ea88c54b0d..975a9aca8a 100644
--- a/django/db/models/aggregates.py
+++ b/django/db/models/aggregates.py
@@ -68,7 +68,7 @@ class Aggregate(Func):
         return []
 
     def as_sql(self, compiler, connection, **extra_context):
-        extra_context['distinct'] = 'DISTINCT ' if self.distinct else ''
+        extra_context['distinct'] = ' '.join(['DISTINCT']) if self.distinct else ''
         if self.filter:
             if connection.features.supports_aggregate_filter_clause:
                 filter_sql, filter_params = self.filter.as_sql(compiler, connection)
```

**变异语义**：使用 `' '.join(['DISTINCT'])` 构造字符串。开发者可能误以为 `join` 会在字符串前后添加空格，但实际上 `join` 只在列表元素之间插入分隔符。单元素列表 `['DISTINCT']` 的 `join` 结果是 `'DISTINCT'`（无尾部空格）。这是一个真实的 Python 语义误解，看起来像是使用 join API 来构造 SQL 关键字序列的合理尝试。

---

### Group D — 替换

**原 mutation**：
```diff
+        c._distinct_reset = True  # BUG: Mark distinct as reset
...
-        extra_context['distinct'] = 'DISTINCT ' if self.distinct else ''
+        extra_context['distinct'] = 'DISTINCT ' if self.distinct and not hasattr(self, '_distinct_reset') else ''
```
**分类**：🔴 必须替换
**理由**：含有 `# BUG: Mark distinct as reset` 注释，明确标注 bug，极不自然，代码审查中立即暴露。

**最终 mutation**：
```diff
diff --git a/django/db/models/aggregates.py b/django/db/models/aggregates.py
index ea88c54b0d..9eab7c0b59 100644
--- a/django/db/models/aggregates.py
+++ b/django/db/models/aggregates.py
@@ -68,7 +68,7 @@ class Aggregate(Func):
         return []
 
     def as_sql(self, compiler, connection, **extra_context):
-        extra_context['distinct'] = 'DISTINCT ' if self.distinct else ''
+        extra_context['distinct'] = extra_context.get('distinct', 'DISTINCT' if self.distinct else '')
         if self.filter:
             if connection.features.supports_aggregate_filter_clause:
                 filter_sql, filter_params = self.filter.as_sql(compiler, connection)
```

**变异语义**：使用 `extra_context.get('distinct', default)` 替代直接赋值。看起来像是防止覆盖调用方可能已提供的 `distinct` 值的防御性编码，但默认值使用了 `'DISTINCT'`（无空格）而非 `'DISTINCT '`。由于正常调用时 `extra_context` 不含 `'distinct'` 键，`get` 总返回缺少空格的默认值。

---

### Group E — 替换

**原 mutation**：
```diff
+        self.distinct_spacing = extra.pop('distinct_spacing', False)
...
-        extra_context['distinct'] = 'DISTINCT ' if self.distinct else ''
+        extra_context['distinct'] = 'DISTINCT ' if self.distinct and self.distinct_spacing else ('DISTINCT' if self.distinct else '')
```
**分类**：🔴 必须替换
**理由**：引入 `distinct_spacing` 参数默认 `False`，使得 `distinct=True` 时总是走 `'DISTINCT'`（无空格）分支，功能上等价于还原原始 bug。

**最终 mutation**：
```diff
diff --git a/django/db/models/aggregates.py b/django/db/models/aggregates.py
index ea88c54b0d..fe29431c69 100644
--- a/django/db/models/aggregates.py
+++ b/django/db/models/aggregates.py
@@ -25,6 +25,7 @@ class Aggregate(Func):
         if distinct and not self.allow_distinct:
             raise TypeError("%s does not allow distinct." % self.__class__.__name__)
         self.distinct = distinct
+        self._distinct_keyword = 'DISTINCT'
         self.filter = filter
         super().__init__(*expressions, **extra)
 
@@ -68,7 +69,7 @@ class Aggregate(Func):
         return []
 
     def as_sql(self, compiler, connection, **extra_context):
-        extra_context['distinct'] = 'DISTINCT ' if self.distinct else ''
+        extra_context['distinct'] = self._distinct_keyword if self.distinct else ''
         if self.filter:
             if connection.features.supports_aggregate_filter_clause:
                 filter_sql, filter_params = self.filter.as_sql(compiler, connection)
```

**变异语义**：在 `__init__` 中将 `'DISTINCT'` 关键字字符串存入实例变量 `_distinct_keyword`，然后 `as_sql` 直接使用该变量。看起来像是将"魔法字符串"提取为实例变量的常见重构模式（子类可以覆盖），但存储的值缺少尾部空格。由于是多位置修改（`__init__` + `as_sql`），在代码审查时容易忽略空格问题。

---

## 新设计 Mutation 说明

所有5个 mutation 均设计为在 `distinct=True` 时产生 `'DISTINCT'`（无尾部空格）的 `distinct` 字段值，使得生成的 SQL 为 `COUNT(DISTINCTCASE WHEN...)` 形式，导致 F2P 测试（`test_count_distinct_expression`）因 SQL 语法错误失败。

P2P 测试（如 `test_count` 中的 `Count("rating", distinct=True)`）不受影响，因为 SQLite 在词法分析时能将 `DISTINCT"field"` 正确解析（`DISTINCT` 是保留关键字，后接引号标识符时无歧义）。

各 mutation 的设计思路：
- **A**：方法提取重构模式，遗漏尾部空格
- **B**：误用 `.rstrip()` "规范化" distinct 字符串
- **C**：误用 `' '.join()` 构造单个关键字（Python join 语义误解）
- **D**：使用 `dict.get` 的防御性编码，但默认值缺少空格
- **E**：将关键字存储为实例变量的重构，跨 `__init__` 和 `as_sql` 两处修改
