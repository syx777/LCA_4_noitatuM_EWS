# django__django-16429

## 问题背景

`USE_TZ=True` 时，对一个月（或更久）之前的 aware datetime 调 `timesince()` 崩溃：`TypeError: can't subtract offset-naive and offset-aware datetimes`。根因：`timesince` 在处理大于一月的间隔时，会用 `d` 的各字段重新构造一个 `pivot` datetime，但构造时没传 `tzinfo`，得到的是 naive datetime；随后 `now - pivot`（now 是 aware）做减法，naive 与 aware 不能相减。Golden patch 在构造 `pivot` 时加上 `tzinfo=d.tzinfo`，使 pivot 与 d/now 一样 aware。

## Golden Patch 语义分析

```python
pivot = datetime.datetime(
    pivot_year,
    pivot_month,
    min(MONTHS_DAYS[pivot_month - 1], d.day),
    d.hour,
    d.minute,
    d.second,
    tzinfo=d.tzinfo,    # ← 新增
)
```
核心语义：**重新构造的 pivot 必须继承原 datetime `d` 的时区信息**，否则 aware 的 `now` 减 naive 的 `pivot` 抛 TypeError。`tzinfo=d.tzinfo`：当 d 是 naive（USE_TZ=False）时 `d.tzinfo` 是 None，pivot 仍 naive，与 naive now 相减正常；当 d 是 aware 时 pivot 也 aware，与 aware now 相减正常。关键是 pivot 的 tz 状态要与 d/now 保持一致。

F2P 测试 `TZAwareTimesinceTests`（`USE_TZ=True` + aware setUp）继承全部 TimesinceTests 用例；其中 `test_thousand_years_ago` 等大间隔用例会走 pivot 构造分支，naive pivot 会触发 TypeError。

## 调用链分析

`timesince(d, now, ...)` 计算 d 与 now 的间隔。对 ≥1 月的间隔，进入"按年月构造 pivot"分支：用 `d.year+years`、`d.month+months` 等重建 `pivot = datetime.datetime(...)`，再 `remaining_time = (now - pivot).total_seconds()`。`now` 由 `timezone.now()` 得到（USE_TZ=True 时 aware）。pivot 构造若不带 tzinfo → naive → `now - pivot` TypeError。`d.tzinfo` 是 pivot 应继承的时区源。任何让 pivot 变 naive（或 tz 错配）的改动都会触发 TypeError 或结果错误。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 删除 `tzinfo=d.tzinfo,` 行，pivot 变 naive，还原 TypeError |
| B | 🔴 必须替换 | 替换 | 原 B 与 A 近似（替换为空行）；改为 `tzinfo=None` 显式置空 |
| C | 🔴 必须替换 | 替换 | 原 C 与 A 字节相同；改为 `tzinfo=d.tzinfo and None`（aware 时恒得 None） |
| D | 🔴 必须替换 | 替换 | 原 D 与 A 字节相同；改为 `tzinfo=getattr(d, "utcoffset", None)`（传方法对象） |
| E | 🟢 高质量 | 保留 | tzinfo 藏到默认关闭的 `preserve_tz` 开关后 |

原 A、C、D 字节完全相同（删除 `tzinfo=d.tzinfo,` 行），B 是把该行替换成空行（实质同删除）。保留 A、E，重做 B、C、D 为不同机制。本 patch 仅一行，故五组围绕 pivot 的 tzinfo 取值分化。

## 各组 Mutation 分析

### Group A — 保留（D1 状态：删除 tzinfo）
```diff
             d.second,
-            tzinfo=d.tzinfo,
         )
```
**变异语义**：删除 `tzinfo=d.tzinfo,`，pivot 用默认 `tzinfo=None` 构造 → naive。aware 的 now 减 naive pivot → TypeError，还原原 bug。保留。

### Group B — 替换（C1 值：显式 None）
**原**：把 `tzinfo=d.tzinfo,` 替换成空行（实质删除）。
**最终 mutation**：
```diff
-            tzinfo=d.tzinfo,
+            tzinfo=None,
```
**变异语义**：显式传 `tzinfo=None`，pivot 强制 naive，与删除等效但形式是"显式写了个错误的 tz 值"。模拟"知道有 tzinfo 参数、却填了 None"。aware 场景 TypeError。与 A（删行）形式不同。

### Group C — 替换（B3 条件：短路恒 None）
**原**：与 A 字节相同（删除 tzinfo 行）。
**最终 mutation**：
```diff
-            tzinfo=d.tzinfo,
+            tzinfo=d.tzinfo and None,
```
**变异语义**：`d.tzinfo and None` 在 d 为 aware（`d.tzinfo` 真值）时求值为 `None`，在 d 为 naive（`d.tzinfo` 是 None）时求值为 `None`——**恒为 None**。看起来像"用 d.tzinfo 做了个条件表达式"，实则无论如何都得 None，pivot 永远 naive。aware 场景 TypeError。模拟"写了个看似有逻辑、实则恒假的布尔短路"。比删行隐蔽——`d.tzinfo` 字样还在。

### Group D — 替换（A1 接口契约：传方法对象）
**原**：与 A 字节相同（删除 tzinfo 行）。
**最终 mutation**：
```diff
-            tzinfo=d.tzinfo,
+            tzinfo=getattr(d, "utcoffset", None),
```
**变异语义**：`getattr(d, "utcoffset", None)` 取到的是 datetime 的 **`utcoffset` 绑定方法对象**（该方法存在，故不走默认 None），把一个 method 传给 `tzinfo` 参数。`datetime.datetime(..., tzinfo=<method>)` 抛 `TypeError: tzinfo argument must be None or of a tzinfo subclass, not type 'builtin_function_or_method'`。模拟"想取偏移却拿成了方法对象/属性名写错"。错误信息不同于原 bug，但同样令 F2P 失败。

### Group E — 保留（E2 隐式→显式开关）
```diff
-def timesince(d, now=None, reversed=False, time_strings=None, depth=2):
+def timesince(d, now=None, reversed=False, time_strings=None, depth=2, preserve_tz=False):
...
-            tzinfo=d.tzinfo,
+            tzinfo=d.tzinfo if preserve_tz else None,
```
**变异语义**：新增参数 `preserve_tz`（默认 False），只有显式传 True 才用 `d.tzinfo`，否则 pivot naive。默认调用不传 → naive → aware 场景 TypeError。模拟"把时区保留做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

原 A、C、D 字节完全相同（删除 `tzinfo=d.tzinfo,` 行），B 把该行替换为空行（实质同删除），五组实际只有"删除"和"开关"两种机制。本次保留 A（删行）、E（默认关闭开关），重做 B（`tzinfo=None` 显式置空）、C（`d.tzinfo and None` 恒 None 的短路）、D（`getattr(d,"utcoffset",None)` 传方法对象触发不同的 TypeError）。本 patch 仅一行，五组围绕 pivot 的 tzinfo 取值分化为"删除 / 显式 None / 恒假短路 / 错误对象 / 默认关闭开关"五个角度。全部实测：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
