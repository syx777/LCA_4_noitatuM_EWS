# matplotlib__matplotlib-22871

## 问题背景

`ConciseDateFormatter` 在绘制 <12 个月、且 x 轴不含一月时，年份不显示在任何地方（应显示在 offset 中）。根因：`format_ticks` 决定是否显示 offset 时，遍历各时间级别（年/月/日…），当某级别有多个 unique 值且 `level < 2`（年或月级）就关闭 offset——但这忽略了"只有当 1（一月/一日）出现在 ticks 中时年份才会显示在 ticks 里、才该关 offset"。Golden patch 把条件改为 `level < 2 and np.any(unique == 1)`：只有 1 在 unique 中时才关 offset，否则保留年份 offset。

## Golden Patch 语义分析

```python
for level in range(5, -1, -1):
    unique = np.unique(tickdate[:, level])
    if len(unique) > 1:
        # if 1 is included in unique, the year is shown in ticks
        if level < 2 and np.any(unique == 1):
            show_offset = False
        break
    elif level == 0:
        ...
```
核心语义：**只有当该级别（年/月）的 unique 值中包含 1（即一月/一日，年份会随之显示在 ticks 里）时，才关闭 offset（`show_offset = False`）**——否则保留 offset 显示年份。关键点：`level < 2 and np.any(unique == 1)` 的 `and` 连接、`np.any(unique == 1)` 用 np.any 归约数组、比较 `== 1`。

F2P 测试 `test_dates.py::test_offset_changes`：新增断言对 7-30 周跨度（<12 月、不含一月），`formatter.get_offset() == '1997'`（年份显示在 offset）。

## 调用链分析

`ConciseDateFormatter.format_ticks(values)` → 遍历 level → `unique = np.unique(tickdate[:, level])` → `if level < 2 and np.any(unique == 1): show_offset = False`。show_offset 决定年份是否进 offset。`==`→`!=`、`and`→`or`、去 np.any、去 unique==1 判断、或藏开关，都会让 offset 显示逻辑错、年份不显示或乱显示。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `unique == 1`→`unique != 1`，判断反转 |
| B | 🟢 高质量 | 保留 | `and`→`or`，level<2 即关 offset |
| C | 🟢 高质量 | 保留 | 去 np.any，`(unique == 1)` 数组真值歧义 |
| D | 🟢 高质量 | 保留 | 去掉 `and np.any(unique==1)`，还原原 bug |
| E | 🟢 高质量 | 保留 | 检查藏到 check_year_in_offset 开关后 |

五组机制各异，全部保留并核验。

## 各组 Mutation 分析

### Group A — 保留（B3 条件反转：== → !=）
```diff
-                if level < 2 and np.any(unique == 1):
+                if level < 2 and np.any(unique != 1):
```
**变异语义**：`np.any(unique == 1)`（是否含 1）反转成 `np.any(unique != 1)`（是否含非 1 值）。判断颠倒——含 1 时不关 offset、不含 1 时反而关。<12 月跨度（不含一月）的 unique 多为非 1 值 → `!= 1` 为真 → 错误地关掉 offset → 年份不显示。F2P 期望 offset='1997' 失败。保留。

### Group B — 保留（B3 逻辑运算符：and→or）
```diff
-                if level < 2 and np.any(unique == 1):
+                if level < 2 or np.any(unique == 1):
```
**变异语义**：`and`→`or`。只要 `level < 2` 就关 offset（无视是否含 1），或只要含 1 就关。<12 月、level<2 时无条件关 offset → 年份不显示。offset 显示条件被放宽到错误。F2P 失败。保留。

### Group C — 保留（C1 类型：去 np.any 数组真值歧义）
```diff
-                if level < 2 and np.any(unique == 1):
+                if level < 2 and (unique == 1):
```
**变异语义**：去掉 `np.any`，`unique == 1` 是个 numpy 布尔数组。`level < 2 and (array)` 对多元素数组取布尔值时抛 `ValueError: The truth value of an array ... is ambiguous`（或单元素时行为偶然），format_ticks 崩溃。模拟"忘了用 np.any 归约数组"。F2P 失败。保留。

### Group D — 保留（B2 删判断：还原 bug）
```diff
-                if level < 2 and np.any(unique == 1):
+                if level < 2:
```
**变异语义**：去掉 `and np.any(unique == 1)`，只留 `if level < 2`——还原原始 bug：不检查 1 是否在 ticks 中就关 offset。<12 月且不含一月时年份 offset 被错误关闭、不显示。F2P 失败。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
-    def __init__(self, ..., *, usetex=None):
+    def __init__(self, ..., *, usetex=None, check_year_in_offset=True):
...
+        self.check_year_in_offset = check_year_in_offset
...
-                if level < 2 and np.any(unique == 1):
+                if level < 2 and self.check_year_in_offset:
```
**变异语义**：把 `np.any(unique == 1)` 判断替换成实例属性 `self.check_year_in_offset`（由新增 __init__ 参数控制，默认 True）。默认构造时该标志恒为 True，使条件退化成 `level < 2 and True`——等价于原始 bug（不检查 unique==1 就关 offset）。`np.any(unique==1)` 的实际检查被开关旁路。F2P 失败。保留。

## 新设计 Mutation 说明

本实例原五组机制已各不相同且全部有效，无重复，故全部保留并逐一核验。五组覆盖"== 反转 / and→or / 去 np.any 数组歧义 / 删判断还原 bug / 默认旁路开关"五个角度，分别作用于比较运算、逻辑连接、数组归约、条件完整性、特性开关五个环节——全部令 <12 月跨度时年份 offset 显示逻辑出错。全部实测（Python 3.9/matplotlib 3.5.0，源码构建 C 扩展）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
