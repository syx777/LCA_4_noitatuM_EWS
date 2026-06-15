# django__django-13121

## 问题背景

在不支持原生 duration 字段的数据库后端（SQLite、MySQL）上，形如 `F('estimated_time') + timedelta(1)` 的表达式（两个 DurationField 操作数相加）会崩溃。原因：

1. `_combine()` 将 `timedelta` 包装为 `DurationValue`（特殊 Value 子类）
2. `CombinedExpression.as_sql()` 检测到 DurationField 就无条件将整个表达式委托给 `DurationExpression`
3. `DurationExpression.compile()` 对 `DurationValue` 跳过 `format_for_duration_arithmetic` 格式化
4. 遗漏格式化导致 `DurationValue.as_sql()` 调用已删除/未实现的 `date_interval_sql()`

Golden patch 的整体解决方案：
- 删除 `DurationValue` 类，改用 `Value(other, output_field=DurationField())`
- 添加 `lhs_type != rhs_type` 条件：**Duration + Duration** 不再委托给 `DurationExpression`（直接用普通算术即可）
- 删除 `DurationExpression.compile()` 中的 `isinstance(DurationValue)` 检查
- 删除各后端的 `date_interval_sql()` 方法

## Golden Patch 语义分析

核心修复逻辑在 `CombinedExpression.as_sql()` 的委托条件：

```python
# base_commit 状态（有 bug）：
if (not connection.features.has_native_duration_field and
        ((lhs_output and lhs_output.get_internal_type() == 'DurationField') or
         (rhs_output and rhs_output.get_internal_type() == 'DurationField'))):
    return DurationExpression(...)  # 无差别委托，Duration+Duration 也委托

# patched 状态（修复后）：
if (
    not connection.features.has_native_duration_field and
    'DurationField' in {lhs_type, rhs_type} and
    lhs_type != rhs_type  ← 关键：只有当两侧类型不同才委托
):
    return DurationExpression(...)  # 只处理 Date+Duration 等混合类型
```

`lhs_type != rhs_type` 的含义：
- **Duration + Duration**：两侧都是 `'DurationField'` → `!=` 为 False → 不委托 → 用普通整数算术 ✓
- **Date/DateTime + Duration**：两侧类型不同 → `!=` 为 True → 委托给 `DurationExpression` ✓

同时 `_combine` 中：`timedelta` → `Value(timedelta, output_field=DurationField())`，让 `Value.as_sql()` 通过 `DurationField.get_db_prep_value()` 将 timedelta 转换为微秒整数，无需 `date_interval_sql()`。

## 调用链分析

```
F('estimated_time') + timedelta(1)
  → Combinable._combine(timedelta(1), '+', reversed=False)
      other = Value(timedelta(1), output_field=DurationField())
      → CombinedExpression(F('estimated_time'), '+', Value(timedelta, DurationField))
  → CombinedExpression.as_sql(compiler, connection)
      lhs_type = 'DurationField'  (from estimated_time DurationField)
      rhs_type = 'DurationField'  (from Value(timedelta, DurationField))
      条件: 'DurationField' in {'DurationField','DurationField'} AND 'DurationField' != 'DurationField'
          = True AND False = False
      → NOT delegated to DurationExpression
      → 普通 combine_expression: estimated_time_col + 86400000000 (microseconds)  ← ✓ 正确
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟡 语义浅层（移除 DurationField 类型，但仍 None） | 替换 | 3个语义浅层mutations，keep floor(3/2)=1个，此为最弱 |
| B | 🔴 必须替换 | 替换 | 反转 lhs_type==rhs_type，破坏 P2P test_duration_with_datetime |
| C | 🔴 必须替换 | 替换 | 与 D 完全相同 diff，直接冗余 |
| D | 🟡 语义浅层（output_field=None） | 保留 | 语义浅层三个保留一个，D 是最直接的 |
| E | 缺失 | 新设计 | E 组缺失 |

语义浅层共 3 个（A/C/D），替换最弱的 2 个（A 是更局限版的 C/D，C 与 D 重复），保留 D。

## 各组 Mutation 分析

### Group A — 替换

**最终 mutation**：
```diff
diff --git a/django/db/models/expressions.py b/django/db/models/expressions.py
index 7987b1e747..156d482f9c 100644
--- a/django/db/models/expressions.py
+++ b/django/db/models/expressions.py
@@ -453,8 +453,7 @@ class CombinedExpression(SQLiteNumericMixin, Expression):
             rhs_type = None
         if (
             not connection.features.has_native_duration_field and
-            'DurationField' in {lhs_type, rhs_type} and
-            lhs_type != rhs_type
+            'DurationField' in {lhs_type, rhs_type}
         ):
             return DurationExpression(self.lhs, self.connector, self.rhs).as_sql(compiler, connection)
```

**分类**：A1（移除必要条件 `lhs_type != rhs_type`）

**变异语义**：移除 `lhs_type != rhs_type` 条件，恢复到"只要有 DurationField 就委托"的原始 bug 逻辑。Duration+Duration 现在也委托给 `DurationExpression`，在 SQLite/MySQL 上会因缺少 `date_interval_sql()` 方法或错误的参数处理而崩溃。Date+Duration 仍然正确委托（P2P 安全）。

---

### Group B — 替换

**最终 mutation**：
```diff
diff --git a/django/db/models/expressions.py b/django/db/models/expressions.py
index 7987b1e747..3542bdad4b 100644
--- a/django/db/models/expressions.py
+++ b/django/db/models/expressions.py
@@ -451,10 +451,9 @@ class CombinedExpression(SQLiteNumericMixin, Expression):
             rhs_type = self.rhs.output_field.get_internal_type()
         except FieldError:
             rhs_type = None
-        if (
-            not connection.features.has_native_duration_field and
-            'DurationField' in {lhs_type, rhs_type} and
-            lhs_type != rhs_type
+        if not connection.features.has_native_duration_field and (
+            ('DurationField' in {lhs_type, rhs_type} and lhs_type != rhs_type) or
+            lhs_type == 'DurationField'
         ):
             return DurationExpression(self.lhs, self.connector, self.rhs).as_sql(compiler, connection)
```

**分类**：B3（添加 `or lhs_type == 'DurationField'` 扩展条件）

**变异语义**：添加 `or lhs_type == 'DurationField'` 子句，使得当 lhs 是 DurationField 时（无论 rhs 类型如何）都委托给 DurationExpression。对 Duration+Duration（lhs='DurationField'）：新子句为 True → 委托 → 在无原生 duration 支持的后端崩溃（F2P 失败）。对 Date+Duration（lhs='DateTimeField'）：原始子句 True → 委托（P2P 安全）。看起来像是"确保 lhs 是 DurationField 时也要做特殊处理"的合理扩展，但引入了 Duration+Duration 的错误委托。

---

### Group C — 替换

**最终 mutation**：
```diff
diff --git a/django/db/models/expressions.py b/django/db/models/expressions.py
index 7987b1e747..837c5ff8fa 100644
--- a/django/db/models/expressions.py
+++ b/django/db/models/expressions.py
@@ -454,7 +454,7 @@ class CombinedExpression(SQLiteNumericMixin, Expression):
         if (
             not connection.features.has_native_duration_field and
             'DurationField' in {lhs_type, rhs_type} and
-            lhs_type != rhs_type
+            rhs_type == 'DurationField'
         ):
             return DurationExpression(self.lhs, self.connector, self.rhs).as_sql(compiler, connection)
```

**分类**：C1（将 `lhs_type != rhs_type` 改为 `rhs_type == 'DurationField'`）

**变异语义**：把类型差异检查替换为直接检查 rhs 是否是 DurationField。对 Duration+Duration：rhs='DurationField' → 条件 True → 委托 DurationExpression → F2P 失败。对 Date+Duration：rhs='DurationField' → 条件 True → 委托（P2P 安全）。表面上看更"显式"，但实际上比 `lhs_type != rhs_type` 的语义更宽：无论 lhs 是什么（包括 DurationField），只要 rhs 是 DurationField 就委托。

---

### Group D — 保留

**原 mutation**（保留）：
```diff
         output_field = None
```
（将 `output_field = DurationField() if isinstance(other, datetime.timedelta) else None` 替换为直接 `output_field = None`）

**变异语义**：移除对 timedelta 的 DurationField 输出类型标注，使 `rhs_type = None`。在 CombinedExpression.as_sql 中：`'DurationField' in {'DurationField', None}` = True，`lhs_type != rhs_type` = True → 委托给 DurationExpression。DurationExpression.compile: rhs 没有 output_field → FieldError → 直接用 compiler.compile(rhs) → Value.as_sql without DurationField conversion → timedelta 作为原始 Python 对象传给数据库适配器 → 通常导致错误或错误结果。

---

### Group E — 新设计

**最终 mutation**：
```diff
diff --git a/django/db/models/expressions.py b/django/db/models/expressions.py
index 7987b1e747..9294a1af47 100644
--- a/django/db/models/expressions.py
+++ b/django/db/models/expressions.py
@@ -57,7 +57,7 @@ class Combinable:
         if not hasattr(other, 'resolve_expression'):
             # everything must be resolvable to an expression
             output_field = (
-                fields.DurationField()
+                fields.DateField()
                 if isinstance(other, datetime.timedelta) else
                 None
             )
```

**分类**：E2（将 timedelta 的 output_field 从 DurationField 改为 DateField）

**变异语义**：将 timedelta 包装为 `Value(timedelta, output_field=DateField())` 而非 DurationField。结果：`rhs_type = 'DateField'`。条件：`'DurationField' in {'DurationField', 'DateField'}` = True，`lhs_type != rhs_type` = True → 委托给 DurationExpression！DurationExpression.compile: rhs 是 Value with DateField output，`get_internal_type() = 'DateField' ≠ 'DurationField'` → 不格式化 → 直接调 compiler.compile → `DateField.get_db_prep_value(timedelta_value)` → 尝试将 timedelta 转换为 date → TypeError 或错误结果 → F2P 失败。P2P test_date_minus_duration 等使用显式 `Value(timedelta, output_field=DurationField())` 的测试不受影响（timedelta 类型标注是显式指定的，不走 `_combine` 路径）。
