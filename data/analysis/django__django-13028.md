# django__django-13028

## 问题背景

当用户有一个 Django Model 带有名为 `filterable` 的 `BooleanField`（值为 `False`），并尝试通过该 Model 实例进行 filter 操作时（如 `ProductMetaData.objects.filter(metadata_type=self.brand_metadata)`），Django 会错误地抛出 `NotSupportedError: ProductMetaDataType is disallowed in the filter clause.`。

根因：`check_filterable(expression)` 函数无条件调用 `getattr(expression, 'filterable', True)`。对于 Model 实例，`filterable` 是一个数据库字段（值为 `False`），而不是 SQL 表达式协议属性。在 `Window` 表达式中，`filterable = False` 是合法的类属性，表示该表达式不能用于 WHERE 子句；但 Model 实例中同名字段的 `False` 值被错误地当作"不可过滤"的标志处理。

Golden patch 通过添加 `hasattr(expression, 'resolve_expression')` 守卫修复此问题——只有 SQL 表达式对象（具有 `resolve_expression` 方法）才会被检查 `filterable` 属性。

## Golden Patch 语义分析

修复的核心逻辑：

```python
# 修复前（base_commit 状态）：
if not getattr(expression, 'filterable', True):
    raise NotSupportedError(...)

# 修复后（patched 状态）：
if (
    hasattr(expression, 'resolve_expression') and   ← 新增守卫
    not getattr(expression, 'filterable', True)
):
    raise NotSupportedError(...)
```

为什么这样修复是正确的：
- SQL 表达式类（`BaseExpression` 子类）都具有 `resolve_expression` 方法，`filterable` 属性表示该表达式是否可以出现在 WHERE 子句中（`Window.filterable = False`）。
- 普通 Django Model 实例没有 `resolve_expression` 方法，它们的 `filterable` 属性（如果存在）是普通的数据库字段值，与表达式协议无关。
- 修复通过 `hasattr(expression, 'resolve_expression')` 区分 SQL 表达式和 Model 实例，只对前者检查 `filterable`。

## 调用链分析

```
QuerySet.filter(extra=e2)
  → QuerySet._filter_or_exclude(False, extra=e2)
  → Query.add_q(Q(extra=e2))
  → Query._add_q(q_object, ...)
  → Query.build_filter(('extra', e2), ...)
      → Query.check_filterable(reffed_expression)  # 对注解引用检查
      → value = Query.resolve_lookup_value(e2, ...)  # e2 无 resolve_expression，直接返回 e2
      → Query.check_filterable(e2)  # 对 filter 右值检查 ← 此处触发 bug
```

`check_filterable` 还有递归调用路径：对于具有 `get_source_expressions()` 的表达式，会递归检查子表达式（用于检测 `Func(Window(...))` 等嵌套情况）。

关键：F2P 测试 `test_field_with_filterable` 在 `check_filterable(e2)` 处触发，其中 `e2` 是 `ExtraInfo` 实例（`filterable=False`，但没有 `resolve_expression`）。

## 替换决策总览

现有 mutations.jsonl 中只有 D 和 E 两组，均为完全相同的 diff（删除 `hasattr` 守卫），等同于还原 base_commit 原始代码（直接冗余）。需要为所有 5 组（A/B/C/D/E）设计全新的高质量 mutation。

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 新设计 | 替换 | 原有 D/E 重复，A 组缺失，需全新设计 |
| B | 新设计 | 替换 | 原有 D/E 重复，B 组缺失，需全新设计 |
| C | 新设计 | 替换 | 原有 D/E 重复，C 组缺失，需全新设计 |
| D | 🔴 必须替换 | 替换 | 直接还原原始 bug 代码（删除 hasattr 守卫）|
| E | 🔴 必须替换 | 替换 | 与 D 完全相同的 diff，直接冗余 |

语义浅层共 0 个，必须替换 2 个（D/E）+ 3 个新增（A/B/C）= 全部重新设计。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：（缺失，需新设计）

**分类**：新设计（A1 - 修改参数语义）

**理由**：A 组缺失，需基于 A1 策略设计。将 `hasattr` 守卫中的字符串从 `'resolve_expression'` 改为 `'filterable'`，看起来像是"先检查对象是否有 filterable 属性再检查其值"的合理逻辑，但实际上 Model 实例也有 `filterable` 字段，导致 Model 实例被错误拦截。

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index d65141b834..39856ca1e7 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1125,7 +1125,7 @@ class Query(BaseExpression):
     def check_filterable(self, expression):
         """Raise an error if expression cannot be used in a WHERE clause."""
         if (
-            hasattr(expression, 'resolve_expression') and
+            hasattr(expression, 'filterable') and
             not getattr(expression, 'filterable', True)
         ):
             raise NotSupportedError(
```

**变异语义**：将检测"是否为 SQL 表达式"的守卫（`resolve_expression`）替换为检测"是否有 filterable 属性"（`filterable`）。Window 表达式（`filterable=False`）和 Model 实例（`filterable=False` DB 字段）都具有 `filterable` 属性，因此两者都会被检查，导致 Model 实例被错误地触发 `NotSupportedError`。代码看起来很合理（"先确认有这个属性再读取"），但混淆了表达式协议属性和 DB 字段。

---

### Group B — 替换

**原 mutation**：（缺失，需新设计）

**分类**：新设计（B2 - 移除空值检查 → 用 try/except 替代 hasattr 守卫）

**理由**：B 组缺失，需基于 B2 策略设计。将整个 `if (hasattr... and not getattr...)` 条件替换为 `try/except AttributeError` 风格，直接访问 `expression.filterable`。这种写法在 Python 中很常见（EAFP 风格），但错误地假设所有没有 `filterable` 属性的对象都不需要被检查，而实际上 Model 实例恰好有 `filterable` 字段，`expression.filterable is False` 会为其评估为 True。

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index d65141b834..ebe6fe9e85 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1124,14 +1124,14 @@ class Query(BaseExpression):
 
     def check_filterable(self, expression):
         """Raise an error if expression cannot be used in a WHERE clause."""
-        if (
-            hasattr(expression, 'resolve_expression') and
-            not getattr(expression, 'filterable', True)
-        ):
-            raise NotSupportedError(
-                expression.__class__.__name__ + ' is disallowed in the filter '
-                'clause.'
-            )
+        try:
+            if expression.filterable is False:
+                raise NotSupportedError(
+                    expression.__class__.__name__ + ' is disallowed in the filter '
+                    'clause.'
+                )
+        except AttributeError:
+            pass
         if hasattr(expression, 'get_source_expressions'):
             for expr in expression.get_source_expressions():
                 self.check_filterable(expr)
```

**变异语义**：用 EAFP（先做后处理）风格替代 LBYL（先检查）风格，直接访问 `expression.filterable` 并捕获 `AttributeError`。Window 表达式（`filterable=False`）→ `expression.filterable is False = True` → 正确抛出（P2P 通过）。Model 实例 e2（DB 字段 `filterable=False`）→ `expression.filterable is False = True` → 错误抛出（F2P 失败）。任何没有 `filterable` 属性的对象 → `AttributeError` 被捕获 → 跳过。代码风格自然，但没有区分表达式协议和 DB 字段。

---

### Group C — 替换

**原 mutation**：（缺失，需新设计）

**分类**：新设计（C1 - 破坏隐式类型转换 → 递归守卫改变）

**理由**：C 组缺失。将递归检查的守卫从 `hasattr(expression, 'get_source_expressions')` 改为 `hasattr(expression, 'filterable')`。看起来像是"有 filterable 属性的对象才需要递归检查子表达式"，但实际上 Model 实例也有 `filterable` 字段，且没有 `get_source_expressions()` 方法，会触发 `AttributeError`。

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index d65141b834..48a1150e01 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1132,7 +1132,7 @@ class Query(BaseExpression):
                 expression.__class__.__name__ + ' is disallowed in the filter '
                 'clause.'
             )
-        if hasattr(expression, 'get_source_expressions'):
+        if hasattr(expression, 'filterable'):
             for expr in expression.get_source_expressions():
                 self.check_filterable(expr)
```

**变异语义**：改变了递归子表达式检查的条件。所有 BaseExpression 子类都有 `filterable` 属性，因此递归行为对表达式对象不变（P2P 通过）。但 Model 实例（如 e2）也有 `filterable` DB 字段，会通过递归守卫检查，然后调用不存在的 `e2.get_source_expressions()` → `AttributeError`（F2P 失败）。错误类型从 `NotSupportedError` 变为 `AttributeError`，但 F2P 测试预期的是正常返回结果，任何异常都会导致测试失败。

---

### Group D — 替换

**原 mutation**（来自 mutations.jsonl）：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index d65141b834..a380ce18e0 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1125,7 +1125,6 @@ class Query(BaseExpression):
     def check_filterable(self, expression):
         """Raise an error if expression cannot be used in a WHERE clause."""
         if (
-            hasattr(expression, 'resolve_expression') and
             not getattr(expression, 'filterable', True)
         ):
             raise NotSupportedError(
```

**分类**：🔴 必须替换（直接还原 base_commit 代码，等同于把 golden patch 逆向还原）

**理由**：直接删除 `hasattr(expression, 'resolve_expression') and` 守卫，完全等同于 base_commit 的原始有 bug 代码。这是对 golden patch 的直接逆操作，属于最典型的"必须替换"情形。

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index d65141b834..9e2d004e36 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1125,7 +1125,7 @@ class Query(BaseExpression):
     def check_filterable(self, expression):
         """Raise an error if expression cannot be used in a WHERE clause."""
         if (
-            hasattr(expression, 'resolve_expression') and
+            (hasattr(expression, 'resolve_expression') or hasattr(expression, 'filterable')) and
             not getattr(expression, 'filterable', True)
         ):
             raise NotSupportedError(
```

**变异语义**：用 `or` 扩展守卫条件，使得"具有 `resolve_expression`"或"具有 `filterable` 属性"的对象都会被检查。Window（有两者）→ 守卫为 True → `filterable=False` → 抛出（P2P 通过）。e2 Model 实例（有 `filterable` DB 字段）→ `hasattr(e2, 'filterable') = True` → 守卫为 True → `filterable=False` → 抛出（F2P 失败）。普通 BaseExpression（`filterable=True`）→ 守卫 True but `not True = False` → 不抛出（P2P 通过）。看起来像是"更完整的检查"，实际引入了对 Model 实例的误判。

---

### Group E — 替换

**原 mutation**（来自 mutations.jsonl）：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index d65141b834..a380ce18e0 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1125,7 +1125,6 @@ class Query(BaseExpression):
     def check_filterable(self, expression):
         """Raise an error if expression cannot be used in a WHERE clause."""
         if (
-            hasattr(expression, 'resolve_expression') and
             not getattr(expression, 'filterable', True)
         ):
             raise NotSupportedError(
```

**分类**：🔴 必须替换（与 Group D 完全相同的 diff，直接冗余）

**理由**：与 D 组完全相同的 diff，重复冗余。

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index d65141b834..6c4cd5e530 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1127,6 +1127,9 @@ class Query(BaseExpression):
         if (
             hasattr(expression, 'resolve_expression') and
             not getattr(expression, 'filterable', True)
+        ) or (
+            not hasattr(expression, 'resolve_expression') and
+            getattr(expression, 'filterable', None) is False
         ):
             raise NotSupportedError(
                 expression.__class__.__name__ + ' is disallowed in the filter '
```

**变异语义**：在原有守卫条件基础上增加一个 `or` 分支，专门捕获"没有 `resolve_expression`"但 `filterable` 精确为 `False` 的对象。Window（有 `resolve_expression`）→ 第一个分支捕获（P2P 通过）。Model 实例 e2（无 `resolve_expression`，`filterable=False`）→ 第二个分支：`not hasattr=True`，`getattr(e2, 'filterable', None) = False`，`is False = True` → 抛出（F2P 失败）。纯 Python 标量（无 `filterable` 属性）→ `getattr(None)` 返回 `None`，`None is False = False` → 不抛出（P2P 通过）。看起来像是对非表达式对象的"额外特殊处理"，实际上把 Model 实例中的 DB 字段 False 值当成了表达式协议的 False 值。

---

## 新设计 Mutation 说明

### 共同分析基础

所有 5 个 mutation 都基于同一个核心洞察：
- Golden patch 的 `hasattr(expression, 'resolve_expression')` 守卫区分了"SQL 表达式对象"（有 `resolve_expression` 方法）和"其他 Python 对象"（如 Model 实例）。
- 破坏这个守卫的方式决定了 mutation 的策略类型。

### 各 Mutation 的设计选择

**Group A (A1)**：将守卫字符串从 `'resolve_expression'` 改为 `'filterable'`，模拟"开发者认为先检查 filterable 属性是否存在再读取它是更正确的写法"的错误。这种错误很难发现，因为逻辑看起来合理，且 Window 表达式的行为不变。

**Group B (B2)**：用 Python EAFP 风格（try/except AttributeError）替代 LBYL 风格（hasattr 检查），模拟"开发者认为捕获 AttributeError 比使用 hasattr 更 Pythonic"的错误。代码结构完全合法，但误把 Model DB 字段的 `False` 当作缺失属性的对立面。

**Group C (C1)**：仅改变递归子表达式检查的守卫（`get_source_expressions` → `filterable`），不改变主检查逻辑，模拟"开发者认为只有具有 filterable 概念的对象才需要递归检查"的错误。实际触发 AttributeError（不同于 A/B/D/E 的 NotSupportedError），失败模式更隐蔽。

**Group D (D3)**：用 `or` 扩展守卫，模拟"开发者认为要全面覆盖所有可能有 filterable 属性的对象"的错误，体现了 D3（引入顺序依赖）的特征——使检查依赖于对象是否有某个属性，而不是对象是否属于 SQL 表达式协议。

**Group E (E2)**：添加一个额外的 `or` 分支专门处理非表达式对象，模拟"开发者试图通过显式分支处理两种情况"的错误，体现了 E2（隐式变显式 → 新分支引入新 bug）的特征。使用 `is False` 精确匹配（比 `not` 更显式），在文本上看起来是更严格、更正确的代码。
