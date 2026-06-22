# django__django-13670

## 问题背景

`DateFormat.y()` 方法用于将年份格式化为两位数字符串（类似 PHP 的 `y` 格式符）。原始实现使用 `str(self.data.year)[2:]` 对年份字符串从第2位开始切片，这对于4位数年份（如 1979 → `'79'`）是正确的，但对于 3 位及以下的年份会失败：
- year=476 → `'6'`（正确应为 `'76'`）
- year=42 → `''`（正确应为 `'42'`）
- year=4 → `''`（正确应为 `'04'`）

Python 的 `datetime.strftime('%y')` 和 PHP 的 `date('y', ...)` 均对此情况做了正确处理（返回带前导零的两位字符串）。

## Golden Patch 语义分析

**修复核心**：将 `str(self.data.year)[2:]`（字符串切片，依赖年份长度 ≥ 3 位）改为 `'%02d' % (self.data.year % 100)`（数学取模 + 格式化，对任意年份均正确）。

修复的两个关键点：
1. `% 100`：取年份后两位数值（数学上正确，不依赖字符串长度）
2. `'%02d'`：强制零填充到两位（确保 0-9 的值输出为 `'00'`-`'09'`）

## 调用链分析

```
dateformat.format(value, format_string)  [module-level convenience]
  └─ DateFormat(value).format(format_string)   [继承自 Formatter]
       └─ re_formatchars.split(formatstr)  [解析格式字符串]
            └─ 对每个格式符调用 self.<char>()
                 └─ DateFormat.y()   ← 被修改的方法
                      └─ self.data.year   [直接读取 datetime 对象的 year 属性]
```

`y()` 是一个无参数的纯计算方法，只读取 `self.data.year`（datetime 对象属性，不可变），不存在状态副作用。`DateFormat` 继承自 `TimeFormat`，`TimeFormat.__init__` 初始化 `self.data` 和 `self.timezone`。

相关方法：
- `Y()` — 返回完整4位年份（`self.data.year`）
- `o()` — 返回 ISO 8601 年份（`self.data.isocalendar()[0]`，与日历年在跨年周时不同）
- `z()` — 返回年中第几天

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 语义浅层 | 保留 | `str(year%100)` 去掉零填充，修改位置关键，通过典型测试但在 year < 10 时失败 |
| B | 必须替换 | 替换 | `% 99` 导致 year=1979 → `'98'`（期望 `'79'`），无法通过 P2P 测试，质量差 |
| C | — | 新增 | 新实例，为 C 组设计高质量 mutation |
| D | — | 新增 | 新实例，为 D 组设计高质量 mutation |
| E | — | 新增 | 新实例，为 E 组设计高质量 mutation |

语义浅层共 1 个（A），替换其中最弱的 floor(1/2) = 0 个：[无需替换，A 保留]

**注**：原始数据只有 A、B 两组，B 为必须替换（P2P 失败），C/D/E 需新设计。

## 各组 Mutation 分析

### Group A — 保留

**原 mutation**：
```diff
diff --git a/django/utils/dateformat.py b/django/utils/dateformat.py
index c4d9d035e4..54462e82d5 100644
--- a/django/utils/dateformat.py
+++ b/django/utils/dateformat.py
@@ -326,7 +326,7 @@ class DateFormat(TimeFormat):
 
     def y(self):
         """Year, 2 digits with leading zeros; e.g. '99'."""
-        return '%02d' % (self.data.year % 100)
+        return str(self.data.year % 100)
 
     def Y(self):
         "Year, 4 digits; e.g. '1999'"
```
**分类**：🟡 语义浅层（保留）
**理由**：去掉 `%02d` 的零填充，使用 `str()` 转换。修改位置在核心逻辑节点（`y()` 唯一实现行），能通过 year=42(`'42'`)、year=476(`'76'`)、year=1979(`'79'`) 等典型测试，只在 year=4（单位数 year%100）下失败，返回 `'4'` 而非 `'04'`。模拟开发者忘记零填充的真实失误，测试不针对单位数年份时难以发现。

**最终 mutation**（保留，与原相同）：
```diff
diff --git a/django/utils/dateformat.py b/django/utils/dateformat.py
index c4d9d035e4..54462e82d5 100644
--- a/django/utils/dateformat.py
+++ b/django/utils/dateformat.py
@@ -326,7 +326,7 @@ class DateFormat(TimeFormat):
 
     def y(self):
         """Year, 2 digits with leading zeros; e.g. '99'."""
-        return '%02d' % (self.data.year % 100)
+        return str(self.data.year % 100)
 
     def Y(self):
         "Year, 4 digits; e.g. '1999'"
```
**变异语义**：去掉零填充，年份个位数（year%100 在 0-9 之间，如 year=4）返回 1 位字符串而非 2 位。绝大多数测试使用 4 位年份（如 1979），不会触发此问题。只有专门测试个位数年份的 F2P 测试会失败。

---

### Group B — 替换

**原 mutation**：
```diff
diff --git a/django/utils/dateformat.py b/django/utils/dateformat.py
index c4d9d035e4..4a501f7a2d 100644
--- a/django/utils/dateformat.py
+++ b/django/utils/dateformat.py
@@ -326,7 +326,7 @@ class DateFormat(TimeFormat):
 
     def y(self):
         """Year, 2 digits with leading zeros; e.g. '99'."""
-        return '%02d' % (self.data.year % 100)
+        return '%02d' % (self.data.year % 99)
 
     def Y(self):
         "Year, 4 digits; e.g. '1999'"
```
**分类**：🔴 必须替换
**理由**：`% 99` 导致 year=1979 → `1979 % 99 = 98`，返回 `'98'` 而非期望的 `'79'`。现有 P2P 测试 `test_time_formats` 直接测试 `dateformat.format(datetime(1979, 7, 8, 22, 00), 'y') == '79'`，会立即失败。这是质量极差的 mutation。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/django/utils/dateformat.py b/django/utils/dateformat.py
index c4d9d035e4..7cc9fd34a8 100644
--- a/django/utils/dateformat.py
+++ b/django/utils/dateformat.py
@@ -326,7 +326,7 @@ class DateFormat(TimeFormat):
 
     def y(self):
         """Year, 2 digits with leading zeros; e.g. '99'."""
-        return '%02d' % (self.data.year % 100)
+        return '%02d' % (self.data.year % 100) if self.data.year >= 100 else '%d' % (self.data.year % 100)
 
     def Y(self):
         "Year, 4 digits; e.g. '1999'"
```
**变异语义**：引入边界条件 `year >= 100`，对年份 < 100 使用 `'%d'`（无零填充）而非 `'%02d'`。year=4 → `'4'`（FAIL），year=42 → `'42'`（pass，因为42已是2位），year=476 → `'76'`（pass），year=1979 → `'79'`（pass）。模拟开发者认为"小于100的年份不需要零填充"的合理但错误的边界判断，只对个位数年份失败。

---

### Group C — 新增

**原 mutation**：（无，新设计）

**分类**：新增（Type & Data Shape）

**最终 mutation**：
```diff
diff --git a/django/utils/dateformat.py b/django/utils/dateformat.py
index c4d9d035e4..691b2e7cef 100644
--- a/django/utils/dateformat.py
+++ b/django/utils/dateformat.py
@@ -326,7 +326,7 @@ class DateFormat(TimeFormat):
 
     def y(self):
         """Year, 2 digits with leading zeros; e.g. '99'."""
-        return '%02d' % (self.data.year % 100)
+        return '%2d' % (self.data.year % 100)
 
     def Y(self):
         "Year, 4 digits; e.g. '1999'"
```
**变异语义**：将 `'%02d'`（零填充）改为 `'%2d'`（空格填充）。对 year%100 >= 10 的情况结果相同（`'76'`、`'42'`、`'79'` 均通过），只对 year%100 < 10 的情况失败：year=4 → `' 4'`（前导空格，而非 `'04'`）。这是典型的格式化类型错误——格式字符串从 `%02d` 到 `%2d` 的单字符差异，代码审查时极易忽略，且大多数测试不覆盖个位数年份场景。

---

### Group D — 新增

**原 mutation**：（无，新设计）

**分类**：新增（I/O & Environment Handling — State/Format Initialization）

**最终 mutation**：
```diff
diff --git a/django/utils/dateformat.py b/django/utils/dateformat.py
index c4d9d035e4..9807f0432d 100644
--- a/django/utils/dateformat.py
+++ b/django/utils/dateformat.py
@@ -326,7 +326,7 @@ class DateFormat(TimeFormat):
 
     def y(self):
         """Year, 2 digits with leading zeros; e.g. '99'."""
-        return '%02d' % (self.data.year % 100)
+        return str(self.data.year % 100).ljust(2, '0')
 
     def Y(self):
         "Year, 4 digits; e.g. '1999'"
```
**变异语义**：用 `.ljust(2, '0')`（左对齐，右侧填零）替代 `'%02d'`（右对齐，左侧填零）。对 year%100 >= 10 的情况结果相同（`'42'`、`'76'`、`'79'`），只对 year%100 < 10 失败：year=4 → `str(4).ljust(2,'0') = '40'`（右侧填零，而非 `'04'`）。模拟开发者混淆 `ljust`/`rjust`/`zfill` 的真实错误，`ljust` 和 `rjust` 名字相近，这种混淆在重构或"优化"代码时很常见。结果是 `'40'` 而非 `'04'`，对 year=4 的 F2P 测试失败，但通过所有典型年份测试。

---

### Group E — 新增

**原 mutation**：（无，新设计）

**分类**：新增（Test-expectation Alignment）

**最终 mutation**：
```diff
diff --git a/django/utils/dateformat.py b/django/utils/dateformat.py
index c4d9d035e4..df380d9f67 100644
--- a/django/utils/dateformat.py
+++ b/django/utils/dateformat.py
@@ -326,7 +326,7 @@ class DateFormat(TimeFormat):
 
     def y(self):
         """Year, 2 digits with leading zeros; e.g. '99'."""
-        return '%02d' % (self.data.year % 100)
+        return '%02d' % (self.data.year % 100) if self.data.year >= 1000 else str(self.data.year % 100)
 
     def Y(self):
         "Year, 4 digits; e.g. '1999'"
```
**变异语义**：以 issue 描述中的 `< 1000` 作为边界：年份 >= 1000 使用正确的零填充格式，年份 < 1000 不做零填充（直接 `str()`）。这直接映射到 issue 标题"doesn't support years < 1000"，模拟开发者读取 issue 后添加了一个针对"< 1000"的特判但处理逻辑依然有误。year=4 → `'4'`（FAIL），year=42 → `'42'`（pass），year=476 → `'76'`（pass），year=1979 → `'79'`（pass）。代码看起来像是"意识到了这个问题并试图修复"，但只在真正的极端情况（年份个位数）下暴露问题。

---

## 新设计 Mutation 说明

### B 替换说明
原 B mutation（`% 99`）违反了 P2P 测试（year=1979 得到 98 而非 79），属于明显错误。新 B 设计针对 "年份 >= 100 才需要零填充" 这一错误直觉：
- 基于代码分析：golden patch 的修复关键是 `%02d`（强制2位零填充）+ `% 100`（取后两位）。
- 错误模式：开发者可能认为"只有四位年份切片时才需要零填充，小年份直接输出就够了"。
- 位置选择：在 `y()` 方法中，最小改动（添加一个三元条件），代码仍然可读。

### C 设计说明
- 基于分析：格式化字符串 `'%02d'` 中的 `0` 是关键——它表示"零填充"而非"空格填充"。
- 错误模式：开发者可能在修改时误打 `'%2d'`（少了 `0`），这是常见的格式字符串笔误。
- 难以发现：`'%2d'` 和 `'%02d'` 在输出 >= 10 的数字时完全相同，只有当数字 < 10 时才有区别（空格 vs 零）。测试如不覆盖 year%100 < 10 的场景，无法检测。

### D 设计说明
- 基于分析：`y()` 方法需要返回**左填充零**的两位字符串。Python 中字符串填充方法有 `zfill()`、`rjust(width, '0')`、`ljust(width, '0')` 等。
- 错误模式：`ljust(2, '0')` 是右侧填充（而非左侧），混淆 `ljust` 与 `rjust` 是真实开发者常犯的错误。
- 失败模式：year=4 → `'40'`（零在右侧），而非期望的 `'04'`（零在左侧）。

### E 设计说明
- 基于分析：issue 标题明确提到"years < 1000"，golden patch 修复了所有年份的两位格式化问题。
- 错误模式：开发者读取 issue 标题，添加了一个"特殊处理 < 1000 的年份"的条件分支，但处理逻辑仍然错误（没有零填充）。
- 难以发现：对于 year=42 和 year=476（F2P 测试中的两个），`str(42%100) = '42'` 和 `str(476%100) = '76'` 恰好是正确答案（不需要零填充）。只有 year=4（单位数年份）才会暴露 bug：`str(4%100) = '4'` 而非 `'04'`。
