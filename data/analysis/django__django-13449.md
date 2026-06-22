# django__django-13449

## 问题背景

SQLite 在处理 `DecimalField` 类型的窗口函数（Window Function）时存在精度问题。`SQLiteNumericMixin` 提供了 `CAST(... AS NUMERIC)` 包装来解决这个问题，其他类（`CombinedExpression`、`Func`）都继承了它。但 `Window` 类只继承自 `Expression`，没有继承 `SQLiteNumericMixin`，导致 SQLite 上 `DecimalField` 类型的窗口函数计算结果不正确。

修复：
1. 让 `Window` 继承 `SQLiteNumericMixin`（获得 CAST 能力）
2. 添加 `as_sqlite()` 方法：若 `output_field` 是 `DecimalField`，则对副本设置内部表达式为 `FloatField`，再调用 `super().as_sqlite()` 获得 `CAST(... AS NUMERIC)` 包装

## Golden Patch 语义分析

```python
# 1. 继承链变更
class Window(SQLiteNumericMixin, Expression):  # 新增 SQLiteNumericMixin

# 2. 新增 as_sqlite 方法
def as_sqlite(self, compiler, connection):
    if isinstance(self.output_field, fields.DecimalField):
        # 先复制，避免修改原对象
        copy = self.copy()
        source_expressions = copy.get_source_expressions()
        # 将内部源表达式标记为 FloatField
        # 这样 super().as_sqlite() (SQLiteNumericMixin) 会检测到 copy 的 output_field
        # 仍为 DecimalField，从而包装 CAST(...AS NUMERIC)
        source_expressions[0].output_field = fields.FloatField()
        copy.set_source_expressions(source_expressions)
        return super(Window, copy).as_sqlite(compiler, connection)
    return self.as_sql(compiler, connection)
```

关键：`SQLiteNumericMixin.as_sqlite` 检查 `self.output_field`，若为 `DecimalField` 则包装 `CAST`。所以 `copy`（其 `output_field` 仍是 `DecimalField`）调用 `super().as_sqlite()` 会正确产生 `CAST(window_sql AS NUMERIC)`。内部源表达式设为 `FloatField` 是为了避免递归 CAST（若内部也是 `DecimalField`，会触发内部的 CAST 形成嵌套）。

## 调用链分析

```
Window(expression, output_field=DecimalField()).as_sqlite(compiler, conn)
  → isinstance(output_field, DecimalField) = True
  → copy = self.copy()
  → source_expressions[0].output_field = FloatField()  # 内部设为Float避免嵌套
  → copy.set_source_expressions(...)
  → super(Window, copy).as_sqlite(compiler, conn)
    → SQLiteNumericMixin.as_sqlite(copy, compiler, conn)
      → sql, params = copy.as_sql(...)  # 生成窗口SQL
      → copy.output_field = DecimalField → sql = 'CAST(%s AS NUMERIC)' % sql
      → return sql, params  # 正确的 CAST 包装
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 语义浅层 | 保留 | 检查 FloatField 而非 DecimalField，永不触发特殊路径 |
| B | 缺失 | 新建 | 用 as_sql() 替代 super().as_sqlite()，跳过 CAST 包装 |
| C | 高质量 | 保留 | 注释掉 FloatField 赋值，内部仍是 DecimalField 导致问题 |
| D | 必须替换（重复C） | 替换 | 与C完全相同，替换为 IntegerField 赋值错误类型 |
| E | 高质量 | 保留 | cast_decimal=False 参数门控，E2 策略 |

## 各组 Mutation 分析

### Group A — 保留

**原 mutation**：
```diff
-        if isinstance(self.output_field, fields.DecimalField):
+        if isinstance(self.output_field, fields.FloatField):
```
**分类**：🟡 语义浅层（保留）
**理由**：修改位置处于 `as_sqlite` 的关键判断。将 `DecimalField` 改为 `FloatField` 后，`Window` 的 `DecimalField` 输出永远不触发特殊 CAST 路径（`FloatField` 输出不常见），直接走 `return self.as_sql(compiler, connection)` 不带 CAST，导致 F2P 失败。

---

### Group B — 新建

**最终 mutation**：
```diff
-            return super(Window, copy).as_sqlite(compiler, connection)
+            return copy.as_sql(compiler, connection)
         return self.as_sql(compiler, connection)
```
**变异语义**：即使正确进入了 `DecimalField` 分支并完成了副本准备，最后调用 `copy.as_sql()` 而非 `super(Window, copy).as_sqlite()`。`as_sql()` 不会应用 `SQLiteNumericMixin` 的 `CAST AS NUMERIC` 包装，只返回原始窗口 SQL。开发者可能认为 `as_sql` 和 `as_sqlite` 等价，或误以为 `copy.as_sql()` 会自动触发继承链中的 SQLite 适配。

---

### Group C — 保留

**原 mutation**：
```diff
-            source_expressions[0].output_field = fields.FloatField()
+            # source_expressions[0].output_field = fields.FloatField()
```
**分类**：🟢 保留
**理由**：不设置内部源表达式的 `output_field`，则内部 `source_expressions[0]` 仍是 `DecimalField`。当 `super().as_sqlite()` 编译 `copy` 时，内部表达式在递归中也会触发 CAST，产生嵌套或错误的 SQL。开发者可能认为不需要修改内部表达式的类型，或在调试时临时注释掉这行忘记恢复。

---

### Group D — 替换（原与C重复）

**最终 mutation**：
```diff
-            source_expressions[0].output_field = fields.FloatField()
+            source_expressions[0].output_field = fields.IntegerField()
```
**变异语义**：将内部源表达式设为 `IntegerField` 而非 `FloatField`。`IntegerField` 不包含小数精度，SQLite 中整数类型无法保留 Decimal 的精确值。虽然外层 `CAST AS NUMERIC` 仍然被应用，但内层已将表达式作为整数处理，最终结果丢失小数部分。F2P 测试中 `Decimal('10.20')` 类的精确值无法正确返回。开发者可能认为"非 Decimal 类型都可以"而随意选择了 `IntegerField`。

---

### Group E — 保留

**原 mutation**：
```diff
-    def as_sqlite(self, compiler, connection):
+    def as_sqlite(self, compiler, connection, cast_decimal=False):
         ...
-            if isinstance(self.output_field, fields.DecimalField):
+            if self.cast_decimal and isinstance(self.output_field, fields.DecimalField):
```
**分类**：🟢 保留
**理由**：E2 策略。`cast_decimal=False` 默认值使 `self.cast_decimal and ...` 永远为 False，CAST 路径永远不激活。`as_sqlite` 直接走到 `return self.as_sql(compiler, connection)`，不做 CAST 包装。这是精确的 E2 策略实现：隐式行为变为需要显式开启的参数，但没有调用者传入该参数。

## 新设计 Mutation 说明

### Group B（新建 B1）
`super(Window, copy).as_sqlite()` 是触发 `SQLiteNumericMixin.as_sqlite()` 的关键调用，它才是施加 `CAST AS NUMERIC` 的地方。改为 `copy.as_sql()` 后，绕过了 MRO 中的 `SQLiteNumericMixin`，所有的副本准备工作白费，最终仍然返回无 CAST 的窗口 SQL。

### Group D（替换重复C）
原 C/D 完全相同。D 替换为使用 `IntegerField` 作为内部源表达式的类型。`IntegerField` 不能表示小数，`SQLite` 中对应 INTEGER 亲和性，`Decimal('10.20')` 在 `IntegerField` 上下文中会被截断为 `10`，丢失精度，导致 F2P 测试的精确 Decimal 比较失败。
