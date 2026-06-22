# matplotlib__matplotlib-24026

## 问题背景

`stackplot` 不应改变 Axes 的 color cycler。`stackplot(colors=['C2','C3','C4'])` 会调 `axes.set_prop_cycle(color=colors)`——而 'C2' 这种 CN 别名（cycle reference）传给 set_prop_cycle 会抛 ValueError，且即使成功也会污染 Axes cycler、破坏跨图颜色同步。Golden patch 改成不调 set_prop_cycle，而用 `itertools.cycle(colors)` 本地循环颜色，绘制时 `facecolor=next(colors)`；colors=None 时用 `axes._get_lines.get_next_color()` 生成器。

## Golden Patch 语义分析

```python
import itertools
...
if colors is not None:
    colors = itertools.cycle(colors)
else:
    colors = (axes._get_lines.get_next_color() for _ in y)
...
coll = axes.fill_between(x, first_line, stack[0, :], facecolor=next(colors), ...)
...
r.append(axes.fill_between(..., facecolor=next(colors), ...))
```
核心语义：**stackplot 应用 `itertools.cycle(colors)` 本地循环颜色、绘制时 `next(colors)` 取色，而非 `axes.set_prop_cycle`（会抛 CN ValueError 并污染 cycler）**。关键点：`itertools.cycle` 包装（使 next 可无限取、支持 CN 别名）、不调 set_prop_cycle、各 fill_between 用 `next(colors)`。

F2P 测试 `test_axes.py::test_stackplot`（image_comparison）：`ax.stackplot(..., colors=["C0","C1","C2"])`，断言渲染与参考图一致（CN 别名不报错、颜色正确）。

## 调用链分析

`ax.stackplot(colors=[...])` → `colors = itertools.cycle(colors)` → 各 `fill_between(facecolor=next(colors))`。若加回 set_prop_cycle（CN 触发 ValueError）、cycle 包装错（顺序/数量错）、不包装（next 对 list 报错）、或校验 raise，stackplot 崩溃或颜色错。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 加回 `set_prop_cycle`，CN 触发 ValueError |
| B | ➕ 补充 | 新增 | `cycle(colors[::-1])` 颜色顺序反转 |
| C | ➕ 补充 | 新增 | `cycle([colors[0]])` 只用首色 |
| D | 🟢 高质量 | 保留 | `colors = colors` 裸列表，next 报错 |
| E | 🟢 高质量 | 保留 | 加回 CN 校验 raise |

原始有 A/D/E，缺 B、C。保留 A、D、E，补充 B（顺序反转）、C（单色循环）。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：加回 set_prop_cycle）
```diff
     if colors is not None:
+        axes.set_prop_cycle(color=colors)
         colors = itertools.cycle(colors)
```
**变异语义**：还原原 bug——在 colors 处理处加回 `axes.set_prop_cycle(color=colors)`。'C0' 等 CN 别名传给 set_prop_cycle 触发 `ValueError: Cannot put cycle reference in prop_cycler`，stackplot 崩溃。F2P 失败。保留。

### Group B — 新增（C1 值：顺序反转）
```diff
-        colors = itertools.cycle(colors)
+        colors = itertools.cycle(colors[::-1])
```
**变异语义**：`cycle(colors)` 改成 `cycle(colors[::-1])`——颜色顺序被反转，各 stacked area 的 facecolor 与参考图不符。不崩溃但渲染错。image_comparison 失败。模拟"取色顺序写反"。新增为 B。

### Group C — 新增（C1 值：单色循环）
```diff
-        colors = itertools.cycle(colors)
+        colors = itertools.cycle([colors[0]])
```
**变异语义**：`cycle(colors)` 改成 `cycle([colors[0]])`——只循环第一个颜色，所有 area 同色。与参考图（多色）不符。模拟"误只取了首个颜色"。比 B（顺序错）更彻底——丢失了所有非首色。F2P 失败。新增为 C。

### Group D — 保留（C1 类型：裸列表）
```diff
-        colors = itertools.cycle(colors)
+        colors = colors
```
**变异语义**：`colors = itertools.cycle(colors)` 改成 `colors = colors`（裸列表）——后续 `next(colors)` 对 list 抛 `TypeError`（list 不是迭代器），stackplot 崩溃。漏了 cycle 包装。F2P 失败。保留。

### Group E — 保留（B2 还原校验：CN raise）
```diff
     if colors is not None:
+        for color in colors:
+            if isinstance(color, str) and len(color) > 1 and color[0] == "C" and color[1:].isdigit():
+                raise ValueError(f"Cannot put cycle reference ({color!r}) in prop_cycler")
         colors = itertools.cycle(colors)
```
**变异语义**：加回对 CN 引用的校验循环——遇到 'C0' 样式颜色就 `raise ValueError`。还原原 bug 的报错行为（golden 正是去掉这种校验、改用 cycle 支持 CN）。F2P 用 ['C0','C1','C2'] 触发 raise。保留。

## 新设计 Mutation 说明

原始有 A/D/E、缺 B、C。本次保留 A（加回 set_prop_cycle 触发 CN ValueError）、D（裸列表 next 报错）、E（加回 CN 校验 raise），补充 B（颜色顺序反转）、C（单色循环）。五组覆盖"set_prop_cycle 崩溃 / 顺序反转 / 单色 / 裸列表崩溃 / CN 校验 raise"五个角度——A/D/E 令 stackplot 崩溃，B/C 不崩溃但颜色渲染错。全部实测（Python 3.9/matplotlib 3.6.0，源码构建 C 扩展+本地 freetype 2.6.1 以支持 image_comparison）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
