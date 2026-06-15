# django__django-13023

## 问题背景

`DecimalField.to_python()` 对字典等不可转换为 `decimal.Decimal` 的类型，抛出的是 `TypeError` 而非 `ValidationError`。原始代码的 `except` 子句只捕获 `decimal.InvalidOperation`，未覆盖 `TypeError`（字典、集合、字节串等触发）和 `ValueError`（列表、元组等触发）。这使得调用方难以将错误追溯到具体的字段。

## Golden Patch 语义分析

**修改前**：
```python
except decimal.InvalidOperation:
    raise exceptions.ValidationError(...)
```

**修改后**：
```python
except (decimal.InvalidOperation, TypeError, ValueError):
    raise exceptions.ValidationError(...)
```

各类型在 `decimal.Decimal(value)` 上引发的异常：
- `str('abc')` → `decimal.InvalidOperation`（原来已处理）
- `dict, set, bytes, complex, object` → `TypeError`（新增捕获）
- `list, tuple` → `ValueError: argument must be a sequence of length 3`（新增捕获）

修复确保所有不合法的输入类型都转化为 `ValidationError`，而非向上传播原始异常。

## 调用链分析

```
Model.save() / form.clean()
  └── Field.clean(value, model_instance)    # Field base class
      └── Field.to_python(value)
          └── DecimalField.to_python(value)  # fields/__init__.py
              → value is None → return None
              → isinstance(value, float) → context.create_decimal_from_float(value)
              → try: decimal.Decimal(value)
                  InvalidOperation → ValidationError (字符串)
                  TypeError → ValidationError (字典/集合/bytes等) ← 新增
                  ValueError → ValidationError (列表/元组) ← 新增
```

`decimal.Context.create_decimal_from_float(value)` 用于浮点数，用上下文精度（`max_digits`）来控制精度，避免 IEEE 754 浮点精度问题。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟡 语义浅层 | 保留 | 移除 `TypeError`，仅保留 `ValueError`，dict/set/bytes 仍抛 TypeError |
| B | 🔴 必须替换 | 替换 | 与 A 完全相同的 diff，直接冗余 |
| C | 🔴 必须替换 | 替换 | 与 D 完全相同的 diff（移除 try/except），直接冗余 |
| D | 🔴 必须替换 | 替换 | 移除 try/except，裸 `return decimal.Decimal(value)`，明显不自然 |
| E | （缺失） | 新设计 | 无原 mutation，设计全新 mutation |

语义浅层共 1 个（A），floor(1/2)=0，无需替换。
必须替换 3 个（B、C、D） + 1 个缺失（E）= 4 个新 mutation。

## 各组 Mutation 分析

### Group A — 保留
**原 mutation**：
```diff
-        except (decimal.InvalidOperation, TypeError, ValueError):
+        except (decimal.InvalidOperation, ValueError):
```
**分类**：🟡 语义浅层（保留）
**理由**：移除 `TypeError` 保留 `ValueError` 和 `InvalidOperation`。对 dict、set、bytes 等触发 `TypeError` 的类型，`TypeError` 不被捕获，向上传播。与黄金 patch 相反，只有移除 `TypeError` 导致部分输入类型仍抛原始异常，而不是全部回退到 base-commit 行为。修改在关键异常捕获处，语义清晰，且与 B（只保留 TypeError）互补，各自对不同输入类型生效，保留价值高。
**最终 mutation**（与原相同）：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index 28374272f4..b543fbe0f2 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -1501,7 +1501,7 @@ class DecimalField(Field):
             return self.context.create_decimal_from_float(value)
         try:
             return decimal.Decimal(value)
-        except (decimal.InvalidOperation, TypeError, ValueError):
+        except (decimal.InvalidOperation, ValueError):
             raise exceptions.ValidationError(
```
**变异语义**：`dict`、`set`、`bytes`、`complex`、`object` 等触发 `TypeError` 的类型不再被捕获为 `ValidationError`，而是向上传播 `TypeError`。`list`、`tuple` 触发 `ValueError` 仍被捕获（行为正确）。`str` 无效字符串触发 `InvalidOperation` 仍被捕获（行为正确）。仅影响 `test_invalid_value` 中 dict/set/bytes/complex/object 这些子测试。

---

### Group B — 替换
**原 mutation**：与 A 完全相同（冗余），**🔴 必须替换**。
**最终 mutation**（只捕获 `TypeError`，不捕获 `InvalidOperation` 和 `ValueError`）：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index 28374272f4..c5560ba2c7 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -1501,7 +1501,7 @@ class DecimalField(Field):
             return self.context.create_decimal_from_float(value)
         try:
             return decimal.Decimal(value)
-        except (decimal.InvalidOperation, TypeError, ValueError):
+        except TypeError:
             raise exceptions.ValidationError(
```
**变异语义**：只捕获 `TypeError`（dict/set/bytes/complex/object），不捕获 `decimal.InvalidOperation`（字符串如 'abc'）和 `ValueError`（list/tuple）。字符串无效值和 list/tuple 值向上传播原始异常而非 `ValidationError`。与 A 互补：A 保留 TypeError、ValueError，B 只保留 TypeError。测试 `test_invalid_value` 中 `'non-numeric string'` 和 `b'non-numeric byte-string'`（bytes→TypeError→OK）以及 `()`/`[]` 测试失败。

---

### Group C — 替换
**原 mutation**：与 D 完全相同（冗余），**🔴 必须替换**。
**最终 mutation**（扩展 float 检查为 `float | int`）：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index 28374272f4..5922c242b3 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -1497,7 +1497,7 @@ class DecimalField(Field):
     def to_python(self, value):
         if value is None:
             return value
-        if isinstance(value, float):
+        if isinstance(value, (float, int)):
             return self.context.create_decimal_from_float(value)
         try:
```
**变异语义**：整数类型（`int`）现在通过 `create_decimal_from_float(int_value)` 路径而非 `decimal.Decimal(int_value)` 路径。`decimal.Context.create_decimal_from_float(1)` 内部先将整数转换为 `float`（`float(1) = 1.0`）再用上下文精度转换。对小整数无影响，但对大整数（如超出 float 53-bit 精度的整数，如 `10**18 + 1`）会发生精度丢失：`float(10**18 + 1) = float(10**18)`，丢失最后的 `+1`。模拟了开发者认为"整数应与浮点数一样用上下文精度处理"的错误假设。new test（TypeError/ValueError）全部通过，仅在大整数精度场景下静默出错。

---

### Group D — 替换
**原 mutation**：裸 `return decimal.Decimal(value)` 移除所有错误处理，**🔴 必须替换**（明显不自然，异常处理被完全删除）。
**最终 mutation**（`InvalidOperation` 直接 re-raise，仅 TypeError/ValueError 转为 ValidationError）：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index 28374272f4..802f3b841b 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -1501,12 +1501,14 @@ class DecimalField(Field):
             return self.context.create_decimal_from_float(value)
         try:
             return decimal.Decimal(value)
-        except (decimal.InvalidOperation, TypeError, ValueError):
-            raise exceptions.ValidationError(
-                self.error_messages['invalid'],
-                code='invalid',
-                params={'value': value},
-            )
+        except (decimal.InvalidOperation, TypeError, ValueError) as e:
+            if isinstance(e, (TypeError, ValueError)):
+                raise exceptions.ValidationError(
+                    self.error_messages['invalid'],
+                    code='invalid',
+                    params={'value': value},
+                )
+            raise
```
**变异语义**：字符串如 `'abc'` 触发 `decimal.InvalidOperation`，由于 `isinstance(e, (TypeError, ValueError))` 为 False，直接 `raise` re-raise 原始异常，不转为 `ValidationError`。dict/set 等触发 `TypeError` 仍正确转为 `ValidationError`。list/tuple 触发 `ValueError` 仍正确转为 `ValidationError`。仅字符串和字节串等触发 `InvalidOperation` 的测试失败。代码结构看起来是有意区分不同异常处理策略，读起来合理但语义错误（`InvalidOperation` 应该同样被转化为 `ValidationError`）。

---

### Group E — 新设计
**原 mutation**：（缺失，全新设计）
**最终 mutation**（将 float 检查移入 except 块作为回退）：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index 28374272f4..4d4e4c3213 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -1497,11 +1497,11 @@ class DecimalField(Field):
     def to_python(self, value):
         if value is None:
             return value
-        if isinstance(value, float):
-            return self.context.create_decimal_from_float(value)
         try:
             return decimal.Decimal(value)
         except (decimal.InvalidOperation, TypeError, ValueError):
+            if isinstance(value, float):
+                return self.context.create_decimal_from_float(value)
             raise exceptions.ValidationError(
```
**变异语义**：`decimal.Decimal(float_value)` 对浮点数**不会抛出异常**——它成功地将 float 转换为 Decimal（但保留完整的 IEEE 754 精度，如 `Decimal('2.0625')` 而非上下文精度的 `Decimal('2.062')`）。因此，`except` 块中的 `if isinstance(value, float): return self.context.create_decimal_from_float(value)` 是**死代码**——float 值永远在 try 中成功返回，不会进入 except。结果：浮点数的精度由 `decimal.Decimal(float)` 的完整 IEEE 754 精度决定，而非 `context.create_decimal_from_float` 的 `max_digits` 精度控制。`test_to_python` 中 `f.to_python(2.0625) == Decimal('2.062')` 失败（实际返回 `Decimal('2.0625')`）；`test_invalid_value` 的 TypeError/ValueError 测试全部通过。模拟了开发者"重构异常处理时将 float 特殊路径挪到 except 中作为回退"的常见代码重组错误，在语法和表面逻辑上完全合理，但理解 `decimal.Decimal(float)` 不抛异常是发现此错误的关键。

## 新设计 Mutation 说明

### Group B 新设计依据
仅保留 `except TypeError` 是与 A 互补的设计：A 保留 InvalidOperation+ValueError（遗漏 TypeError），B 只保留 TypeError（遗漏 InvalidOperation+ValueError）。两者各自只处理部分类型，覆盖的失败场景不同。B 的场景：dict/set/bytes/complex/object 正确处理，但 'abc'、[]、() 仍抛原始异常。这个错误模拟了开发者"只修了自己注意到的类型（dict）但遗漏了其他类型"的场景。

### Group C 新设计依据
整数被路由到浮点精度路径是一个真实且隐蔽的语义错误。开发者可能认为"整数和浮点都需要精度控制，应走同一路径"，但实际上整数应直接用 `decimal.Decimal(int)` 获得精确值。大整数精度丢失是静默的（无错误，只是结果不精确），且只在整数超出 float 精度时才触发，极难被简单测试发现。

### Group D 新设计依据
将 `InvalidOperation` 与 `TypeError/ValueError` 区别对待，是开发者可能犯的语义错误：认为"类型错误和值错误需要用户友好的 ValidationError，但 Python decimal 库的 InvalidOperation 是内部错误，应该透传"。这个假设看起来有一定道理，但实际上所有这些异常都代表输入值非法，都应转化为 ValidationError。多行改动（添加 `as e` + `isinstance` 检查 + `raise`）使修改看起来是有意为之的精细化处理，而非简单错误。

### Group E 新设计依据
理解 `decimal.Decimal(float)` 不抛异常（而是成功转换）是关键：代码审查者必须知道 Python `decimal` 模块的这个行为，才能发现 except 块中的 `isinstance(value, float)` 是死代码。代码重构时将"前置检查"挪到"异常回退"中是一个常见的重构思路，表面上看减少了特殊分支，逻辑更统一，但实际上改变了浮点数的处理精度。
