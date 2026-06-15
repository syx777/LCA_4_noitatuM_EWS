# django__django-13012

## 问题背景

`ExpressionWrapper` 包装常量表达式（如 `Value(3)`）时，Django 错误地将该常量放入 `GROUP BY` 子句，导致 PostgreSQL 抛出 `ProgrammingError: aggregate functions are not allowed in GROUP BY`。

根本原因：`ExpressionWrapper` 未重写 `get_group_by_cols()` 方法，使用了基类 `Expression.get_group_by_cols()` 的默认实现，该实现在非聚合情形下返回 `[self]`（即 ExpressionWrapper 自身）。而 `Value(3)` 自身有 `get_group_by_cols()` 重写会返回 `[]`（常量不参与 GROUP BY），但此重写在 ExpressionWrapper 包装后被绕过了。

## Golden Patch 语义分析

**修改**：在 `ExpressionWrapper` 中添加 `get_group_by_cols()` 方法，委托给内部 `self.expression`：

```python
def get_group_by_cols(self, alias=None):
    return self.expression.get_group_by_cols(alias=alias)
```

**语义**：将 GROUP BY 列的计算完全交给被包装的表达式本身。
- `ExpressionWrapper(Value(3))` → `Value(3).get_group_by_cols()` → `[]`（常量，不参与 GROUP BY）
- `ExpressionWrapper(Lower(Value('f')))` → `Lower(...).get_group_by_cols()` → 基类实现 → `[Lower(Value('f'))]`（非常量，参与 GROUP BY）

关键设计：不是手动判断 "是否是常量"，而是正确地向下委托，让各表达式类型自己负责 GROUP BY 语义。

## 调用链分析

```
QuerySet.annotate(expr_res=ExpressionWrapper(...)).values(...).annotate(sum=Sum(...))
  └── Query.resolve_expression(...)
  └── SQLCompiler.get_group_by()              # compiler.py
      └── annotation.get_group_by_cols()      # 对每个注解调用
          → ExpressionWrapper.get_group_by_cols()  # (缺失前: 调用基类)
              ← Expression.get_group_by_cols() [基类]
                → if not contains_aggregate: return [self]  # 返回 ExpressionWrapper 自身
              ← 修复后:
                → self.expression.get_group_by_cols(alias=alias)
                    ← Value.get_group_by_cols() → []        # 常量不参与 GROUP BY
                    ← Lower.get_group_by_cols() → [Lower]  # 函数参与 GROUP BY
```

各类 `get_group_by_cols()` 行为：
- `Value.get_group_by_cols()` → `[]`（常量，明确排除）
- `Expression.get_group_by_cols()` → `[self]` 或递归子表达式（基类默认）
- `Aggregate.get_group_by_cols()` → 递归子表达式（聚合函数内部分组）
- `Col.get_group_by_cols()` → `[self]`（列引用，参与 GROUP BY）

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | （缺失） | 新设计 | 无原 mutation，设计全新 mutation |
| B | 🟡 语义浅层 | 保留 | `hasattr` 检查始终为 True → 始终返回 `[]`，破坏非常量情形 |
| C | （缺失） | 新设计 | 无原 mutation，设计全新 mutation |
| D | 🔴 必须替换 | 替换 | `# self.expression = expression` 注释掉赋值，明显人工痕迹 |
| E | 🟡 语义浅层（替换） | 替换 | `return [self.expression]` 直接返回，不委托内部 get_group_by_cols，常量仍入 GROUP BY |

语义浅层共 2 个（B、E），替换最弱的 floor(2/2)=1 个：B 始终返回 `[]`，E 只在常量时错误（非常量偶然正确）。E 影响范围更窄，但与 B 相比不具备"正确处理非常量"的优势，且 B 通过否定委托破坏了更广泛的情形（所有非常量非聚合的 GROUP BY 均被清空）。**替换 E**（影响范围更集中，模拟了"返回容器元素"而非"递归委托"的常见实现错误）。

必须替换 D + 2 个新设计（A、C）。

## 各组 Mutation 分析

### Group A — 新设计
**原 mutation**：（缺失，全新设计）
**最终 mutation**：
```diff
diff --git a/django/db/models/expressions.py b/django/db/models/expressions.py
index 6bd1471692..f7cdaf0802 100644
--- a/django/db/models/expressions.py
+++ b/django/db/models/expressions.py
@@ -864,7 +864,7 @@ class ExpressionWrapper(Expression):
         return [self.expression]
 
     def get_group_by_cols(self, alias=None):
-        return self.expression.get_group_by_cols(alias=alias)
+        return self.expression.get_group_by_cols()
```
**变异语义**：调用内部表达式的 `get_group_by_cols()` 时不传递 `alias` 参数（默认为 None）。对 `Value(3)` 和 `Lower(Value('f'))` 等简单表达式，`alias=None` 是默认值，行为等价。但对于引用了带别名的列（`Col` with alias）的情形，alias 参数影响 GROUP BY 中使用原表名还是别名。ExpressionWrapper 包装了一个使用 `alias` 的 Col 表达式时，GROUP BY 会使用错误的列标识，导致 SQL 引用了不存在的别名或错误的列。测试的简单情形（Value 和 Lower）通过；带别名列表达式的情形静默错误。模拟了开发者复制粘贴时漏掉 `alias=alias` 的 kwarg 透传错误。

---

### Group B — 保留
**原 mutation**：
```diff
+        if hasattr(self.expression, "get_group_by_cols"):
+            return []
         return self.expression.get_group_by_cols(alias=alias)
```
**分类**：🟡 语义浅层（保留）
**理由**：`hasattr(..., "get_group_by_cols")` 对所有继承自 `Expression` 的表达式均为 True（基类已定义该方法），因此这个条件始终成立，`get_group_by_cols` 始终返回 `[]`。`test_empty_group_by`（Value(3)→[]）通过，`test_non_empty_group_by`（Lower→[Lower]）失败。语义浅层，单行条件替换，但修改位置在关键委托路径上，且模拟了开发者"优化"但做了错误检测的真实错误。B 对所有非常量表达式均有影响，范围广。
**最终 mutation**（与原相同）：
```diff
diff --git a/django/db/models/expressions.py b/django/db/models/expressions.py
index 6bd1471692..ec43e44e3a 100644
--- a/django/db/models/expressions.py
+++ b/django/db/models/expressions.py
@@ -864,6 +864,8 @@ class ExpressionWrapper(Expression):
         return [self.expression]
 
     def get_group_by_cols(self, alias=None):
+        if hasattr(self.expression, "get_group_by_cols"):
+            return []
         return self.expression.get_group_by_cols(alias=alias)
```
**变异语义**：因为 `get_group_by_cols` 方法在基类 `Expression` 上已定义，所有 expression 对象都有该方法，`hasattr` 始终为 True，导致 `get_group_by_cols` 始终返回空列表。所有被 ExpressionWrapper 包装的表达式都不会出现在 GROUP BY 中，包括本应出现的字段引用和函数调用。

---

### Group C — 新设计
**原 mutation**：（缺失，全新设计）
**最终 mutation**：
```diff
diff --git a/django/db/models/expressions.py b/django/db/models/expressions.py
index 6bd1471692..e8943d56aa 100644
--- a/django/db/models/expressions.py
+++ b/django/db/models/expressions.py
@@ -864,7 +864,7 @@ class ExpressionWrapper(Expression):
         return [self.expression]
 
     def get_group_by_cols(self, alias=None):
-        return self.expression.get_group_by_cols(alias=alias)
+        return super().get_group_by_cols(alias=alias)
```
**变异语义**：调用父类 `Expression.get_group_by_cols()` 而非委托给内部表达式。父类实现：若 `self.contains_aggregate` 为 False（ExpressionWrapper 不是聚合），则返回 `[self]`（ExpressionWrapper 对象本身）。效果等同于没有重写 `get_group_by_cols` 的原始 bug：常量 ExpressionWrapper 仍然出现在 GROUP BY 中（只是以 ExpressionWrapper 对象而非 inner expression 形式）。SQL 生成时 ExpressionWrapper.as_sql 委托给 inner expression，结果是 GROUP BY 中包含了常量表达式的 SQL，与 bug 重现。模拟了开发者写 `super()` 时以为"父类实现是正确的"，但实际上正是需要覆盖的情形。

---

### Group D — 替换
**原 mutation**：
```diff
-        self.expression = expression
+        # self.expression = expression
```
**分类**：🔴 必须替换
**理由**：注释掉赋值语句是明显的人工痕迹，代码审查会立刻发现。
**最终 mutation**（替换，使用 `contains_aggregate` 判断替代正确委托）：
```diff
diff --git a/django/db/models/expressions.py b/django/db/models/expressions.py
index 6bd1471692..fd157985fc 100644
--- a/django/db/models/expressions.py
+++ b/django/db/models/expressions.py
@@ -864,6 +864,8 @@ class ExpressionWrapper(Expression):
         return [self.expression]
 
     def get_group_by_cols(self, alias=None):
+        if not self.expression.contains_aggregate:
+            return []
         return self.expression.get_group_by_cols(alias=alias)
```
**变异语义**：使用 `contains_aggregate` 来判断是否应排除表达式的 GROUP BY 列——`contains_aggregate=False` 时返回 `[]`，否则委托。这个逻辑从表面上看合理（"非聚合表达式不需要 GROUP BY"），但实际上是错误的：非聚合表达式（如 `Lower('field_name')`，`concat` 等函数）也需要出现在 GROUP BY 中。`contains_aggregate=False` 不等价于"常量"——常量的正确标志是 `Value.get_group_by_cols()` 返回 `[]`，而非 `contains_aggregate` 属性。结果：`ExpressionWrapper(Lower(F('name')))` 的 GROUP BY 被清空，导致 SQL 聚合错误。测试 `test_empty_group_by` 通过，`test_non_empty_group_by` 失败。

---

### Group E — 替换
**原 mutation**：
```diff
-        return self.expression.get_group_by_cols(alias=alias)
+        return [self.expression]
```
**分类**：🟡 语义浅层（替换）
**理由**：直接返回 `[self.expression]` 而非委托给内部表达式的 `get_group_by_cols`。对 `Value(3)`：应返回 `[]` 但返回 `[Value(3)]`，常量仍入 GROUP BY（F2P 失败）。对 `Lower(Value('f'))`：基类 `Lower.get_group_by_cols()` 也返回 `[self]`，与直接 `[self.expression]` 偶然相同，所以 `test_non_empty_group_by` 也通过。此 mutation 影响面较窄，仅在常量表达式时失败。相比 B（所有非常量均失效），E 的破坏范围更小。
**最终 mutation**（替换，改为 `cols if cols else [self]`）：
```diff
diff --git a/django/db/models/expressions.py b/django/db/models/expressions.py
index 6bd1471692..19772c8340 100644
--- a/django/db/models/expressions.py
+++ b/django/db/models/expressions.py
@@ -864,7 +864,8 @@ class ExpressionWrapper(Expression):
         return [self.expression]
 
     def get_group_by_cols(self, alias=None):
-        return self.expression.get_group_by_cols(alias=alias)
+        cols = self.expression.get_group_by_cols(alias=alias)
+        return cols if cols else [self]
```
**变异语义**：委托调用是正确的，但当内部表达式的 `get_group_by_cols()` 返回空列表时（即常量表达式情形），回退到 `[self]`（ExpressionWrapper 自身）。效果：`Value(3)` 正确返回 `[]`，但随即被 `[self]` 替代，重新引入了常量入 GROUP BY 的 bug。对非常量（如 `Lower(Value('f'))`）：返回非空列表，`if cols` 为 True，不触发回退，行为正确。精确地模拟了开发者"防御性地添加回退"的错误：认为"空的 GROUP BY 列意味着出错了，需要回退"，而实际上空返回是正确的（常量不应该入 GROUP BY）。

## 新设计 Mutation 说明

### Group A 新设计依据
`get_group_by_cols(alias=alias)` 的 `alias` 参数在大多数简单场景下不影响结果，因为默认值就是 None。但在复杂查询中（带别名的 subquery 引用），`alias` 用于控制 GROUP BY 中用原始列名还是别名。漏掉 `alias=alias` 传递是一个典型的"参数透传遗漏"错误，在代码审查中极难发现（函数签名相同，只是缺少了 kwarg 转发）。测试用例 test_empty_group_by 和 test_non_empty_group_by 不涉及 alias，均通过；只有带 alias 的实际查询才会失败。

### Group C 新设计依据
`super().get_group_by_cols(alias=alias)` 在阅读时非常自然——许多子类会调用 super() 进行 GROUP BY 计算。但对于 ExpressionWrapper 这个特殊类，恰好需要绕过自身而直接委托给内部表达式。`super()` 调用会返回 `[self]`（ExpressionWrapper 对象），而内部 expression 的 `get_group_by_cols()` 可能返回不同结果。这个错误精确重现了 bug，且代码形式非常惯用（super() 调用），需要理解 ExpressionWrapper 的特殊语义才能发现。

### Group E 新设计依据
"结果为空时回退到默认值"是防御性编程中的常见模式，但在这里语义完全错误：空列表是常量表达式的**正确返回值**，不是"出错"的信号。`return cols if cols else [self]` 看起来像合理的防御代码，但恰好将修复了 bug 的情形（常量→空列表）重新变回错误（常量→[self]）。模拟了开发者认为"没有 GROUP BY 列是异常情况"的误解。
