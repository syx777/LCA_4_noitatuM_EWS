# django__django-13363

## 问题背景

`TruncDate` 和 `TruncTime` 均继承自 `TruncBase`，而 `TruncBase` 继承了 `TimezoneMixin`，理论上支持通过 `tzinfo` 参数指定时区进行日期/时间截断。然而 `TruncDate.as_sql()` 和 `TruncTime.as_sql()` 都直接调用 `timezone.get_current_timezone_name()`，完全忽略了传入的 `tzinfo` 参数。修复方案：将两处硬编码调用替换为 `self.get_tzname()`（`TimezoneMixin` 已提供的方法，正确处理 `self.tzinfo`）。

## Golden Patch 语义分析

修复的核心是将 `TruncDate.as_sql()` 和 `TruncTime.as_sql()` 中：
```python
tzname = timezone.get_current_timezone_name() if settings.USE_TZ else None
```
替换为：
```python
tzname = self.get_tzname()
```

`get_tzname()`（定义在 `TimezoneMixin`）已正确实现：若 `USE_TZ` 启用，且 `self.tzinfo` 为 None，则用全局时区；若 `self.tzinfo` 有值，则用 `_get_timezone_name(self.tzinfo)`。原有代码虽然 `TruncYear` 等其他 Trunc 类都通过 `TruncBase.as_sql()` 正确调用 `self.get_tzname()`，但 `TruncDate` 和 `TruncTime` 重写了 `as_sql()` 却遗漏了这一逻辑。

## 调用链分析

```
TruncDate('start_datetime', tzinfo=melb).as_sql(compiler, connection)
  → self.get_tzname()  ← 修复后调用
    → TimezoneMixin.get_tzname()
      → if self.tzinfo is None: timezone.get_current_timezone_name()
      → else: timezone._get_timezone_name(self.tzinfo)  ← 使用 melb
  → connection.ops.datetime_cast_date_sql(lhs, tzname)

TruncBase.__init__(tzinfo=melb)
  → self.tzinfo = tzinfo  ← 正确存储到实例属性
  → 覆盖 TimezoneMixin.tzinfo = None（类属性）
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 缺失 | 新建 | 数据集中无 A 组，设计不对称修复（TruncDate 修了 TruncTime 没修）|
| B | 语义浅层 | 保留 | 关键逻辑分支反转，边界清晰，模拟真实逻辑错误 |
| C | 缺失 | 新建 | 数据集中无 C 组，设计 C1 类型参数混淆 |
| D | 必须替换 | 替换 | 含 `# D3 bug: removed tzinfo check` 注释，人工痕迹明显 |
| E | 必须替换 | 替换 | 含 `# Bug: ignore tzinfo parameter` 注释，人工痕迹明显 |

语义浅层 1 个（B），保留。D、E 必须替换，共替换 2 个，新建 A、C。

## 各组 Mutation 分析

### Group A — 新建

**最终 mutation**：
```diff
 class TruncTime(TruncBase):
     def as_sql(self, compiler, connection):
         lhs, lhs_params = compiler.compile(self.lhs)
-        tzname = self.get_tzname()
+        tzname = timezone.get_current_timezone_name() if settings.USE_TZ else None
         sql = connection.ops.datetime_cast_time_sql(lhs, tzname)
```
**变异语义**：golden patch 同时修复了 TruncDate 和 TruncTime，本 mutation 仅保留 TruncDate 的修复，将 TruncTime 还原为硬编码的 `get_current_timezone_name()`。开发者可能只修了先看到的那个，忘记同样的问题也存在于 TruncTime。F2P 测试中 `melb_time`/`pacific_time` 断言失败，而 `melb_date`/`pacific_date` 通过，使错误难以一眼定位。

---

### Group B — 保留

**原 mutation**：
```diff
-            if self.tzinfo is None:
+            if self.tzinfo is not None:
                 tzname = timezone.get_current_timezone_name()
             else:
                 tzname = timezone._get_timezone_name(self.tzinfo)
```
**分类**：🟡 语义浅层（保留）
**理由**：修改位置处于 `get_tzname()` 的核心逻辑判断，`is None` → `is not None` 将两个分支完全交换：提供了 `tzinfo` 时反而使用全局时区，未提供时调用 `_get_timezone_name(None)`（可能返回 None 或崩溃）。这是一个真实的逻辑错误，模拟了开发者对 `is None` vs `is not None` 的混淆。

---

### Group C — 新建

**最终 mutation**：
```diff
             else:
-                tzname = timezone._get_timezone_name(self.tzinfo)
+                tzname = timezone._get_timezone_name(timezone.get_current_timezone())
```
**变异语义**：当 `self.tzinfo` 有值时，用 `timezone.get_current_timezone()`（返回当前激活的时区对象）代替 `self.tzinfo` 传入 `_get_timezone_name`。这模拟了开发者混淆"当前激活时区"与"实例存储的时区"的错误：调用结果返回全局时区名称，与直接使用 `get_current_timezone_name()` 效果相同，所以在全局时区与传入时区不一致时失败，但代码看起来合理（`_get_timezone_name` 确实接受时区对象）。

---

### Group D — 替换（原：🔴 必须替换）

**原 mutation**（含 `# D3 bug: removed tzinfo check` 注释，不自然）

**最终 mutation**：
```diff
     def __init__(self, expression, output_field=None, tzinfo=None, is_dst=None, **extra):
-        self.tzinfo = tzinfo
+        self._tzinfo = tzinfo
```
**变异语义**：`TruncBase.__init__()` 将 `tzinfo` 参数存储为 `self._tzinfo` 而非 `self.tzinfo`。`TimezoneMixin` 中定义了类属性 `tzinfo = None`，`get_tzname()` 读取 `self.tzinfo`，但实例只有 `_tzinfo`，因此读到的始终是类属性 `None`，始终使用 `get_current_timezone_name()`。同时 `convert_value()` 中的 `self.tzinfo` 也受影响（时区转换逻辑同样失效）。开发者可能习惯用下划线前缀表示"private"存储，而未意识到父类直接读取 `self.tzinfo`。

---

### Group E — 替换（原：🔴 必须替换）

**原 mutation**（含 `# Bug: ignore tzinfo parameter` 注释，不自然）

**最终 mutation**：
```diff
-            if self.tzinfo is None:
+            if self.tzinfo is None or not getattr(self, 'use_tzinfo', False):
                 tzname = timezone.get_current_timezone_name()
```
**变异语义**：在 `get_tzname()` 中增加条件 `not getattr(self, 'use_tzinfo', False)`，使 `self.tzinfo` 分支只在类或实例设置 `use_tzinfo=True` 时才生效。由于没有任何子类设置该属性，`getattr` 默认返回 `False`，`not False` 为 `True`，整个条件始终为 `True`，`get_current_timezone_name()` 永远被调用。这模拟了 E2 策略：开发者引入了一个看似灵活的特性开关，但因为默认值错误（`False` 意味着"不用 tzinfo"），默认行为被破坏。

## 新设计 Mutation 说明

### Group A（A2 — 不完整修复）
基于代码分析：golden patch 同时修复了 `TruncDate.as_sql()` 和 `TruncTime.as_sql()`，本 mutation 仅撤销 `TruncTime` 的修复，保留 `TruncDate` 的修复。这模拟了开发者在 review 类似代码时只注意到了第一个位置，遗漏了相似结构的第二个位置。

### Group C（C1 — 参数混淆）
基于对 `TimezoneMixin.get_tzname()` 的分析：`_get_timezone_name` 同时接受 `pytz.timezone` 对象（`self.tzinfo`）和 `timezone.get_current_timezone()` 的返回值，函数签名相同。开发者可能在修复时误认为应该传入"当前时区对象"而非"存储的时区属性"，导致永远使用全局时区。
