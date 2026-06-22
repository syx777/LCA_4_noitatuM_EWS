# mwaskom__seaborn-3187

## 问题背景

大数值范围的 legend 取值错误。0.12.1 起，用 `ScalarFormatter` 带 offset 生成的大数 legend（如 1e6 量级的 body_mass_mg）显示时丢掉了乘性 offset，标签变成小数。根因：seaborn 用 ScalarFormatter 生成 legend tick 标签时未禁用 offset/科学计数——而 legend 没有地方展示 offset。Golden patch 两处禁用 offset：`_core/scales.py`（objects 接口）和 `utils.py`（relational 接口的 `locator_to_legend_entries`），各调 `set_useOffset(False)` + `set_scientific(False)`。

## Golden Patch 语义分析

```python
# scales.py
if hasattr(axis.major.formatter, "set_useOffset"):
    axis.major.formatter.set_useOffset(False)
if hasattr(axis.major.formatter, "set_scientific"):
    axis.major.formatter.set_scientific(False)

# utils.py
formatter = mpl.ticker.ScalarFormatter()
formatter.set_useOffset(False)
formatter.set_scientific(False)
```
核心语义：**生成 legend 标签的 ScalarFormatter 必须 `set_useOffset(False)` 和 `set_scientific(False)`，使大数值完整显示而非被 offset 拆分**。两个代码路径（objects 的 scales.py、relational 的 utils.py）都要禁用。关键点：两处都调 set_useOffset(False)。

F2P 测试：`test_plot.py::TestLegend::test_legend_has_no_offset`（objects 路径，color=x+1e8，断言每个 legend 文本 float > 1e7）+ `test_relational.py::TestRelationalPlotter::test_legend_has_no_offset`（relational 路径，hue=z+1e8）。

## 调用链分析

objects 接口 `Plot(...).add(...).plot()` → `ContinuousBase` 的 spacer → scales.py 的 formatter 禁用 offset → format_ticks。relational 接口 `relplot(hue=...)` → `locator_to_legend_entries`（utils.py）→ ScalarFormatter 禁用 offset。任一路径未禁用 offset，大数 legend 标签被 offset 拆成小数，F2P 的 `float(text) > 1e7` 失败。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | scales.py `set_useOffset(False)`→`(True)` |
| B | 🟢 高质量 | 重做 | utils.py `set_useOffset(False)`→`(True)` |
| C | 🟢 高质量 | 保留 | scales.py set_useOffset 改 `pass` |
| D | 🟢 高质量 | 重做 | utils.py 删除 set_useOffset(False) |
| E | 🟢 高质量 | 保留 | scales.py offset 禁用藏到 _DISABLE_OFFSET 开关后 |

原始 B/C/D 都作用于 scales.py 同一处（禁用 set_useOffset），机制趋同。保留 A、C、E（scales.py），重做 B、D 为 utils.py 路径（覆盖 relational F2P 的另一文件）。

## 各组 Mutation 分析

### Group A — 保留（C1 值：scales.py useOffset True）
```diff
-                axis.major.formatter.set_useOffset(False)
+                axis.major.formatter.set_useOffset(True)
```
**变异语义**：scales.py 里 `set_useOffset(False)` 改成 `(True)`——objects 接口的 legend formatter 启用 offset，大数值（1e8）标签被减去 offset 显示成小数。`test_plot` 的 `float(text) > 1e7` 失败。保留。

### Group B — 重做（C1 值：utils.py useOffset True）
**原**：作用于 scales.py（与 C/D 趋同）。
**最终 mutation**（`seaborn/utils.py`）：
```diff
-        formatter.set_useOffset(False)
+        formatter.set_useOffset(True)
         formatter.set_scientific(False)
```
**变异语义**：utils.py 的 `locator_to_legend_entries` 里 `set_useOffset(False)` 改成 `(True)`——relational 接口（relplot/scatterplot）的 legend 启用 offset，大数值标签错。作用于另一文件、覆盖 relational F2P。与 A（objects 路径）对称。重做为 B。

### Group C — 保留（B2 scales.py 改 pass）
```diff
             if hasattr(axis.major.formatter, "set_useOffset"):
-                axis.major.formatter.set_useOffset(False)
+                pass  # set_useOffset(False) removed
```
**变异语义**：scales.py 里把 `set_useOffset(False)` 调用改成 `pass`——不禁用 offset，formatter 保持默认（带 offset），objects 接口 legend 大数值标签错。删除禁用调用（保留 if 壳）。`test_plot` 失败。保留。

### Group D — 重做（B2 utils.py 删除调用）
**原**：作用于 scales.py（与 C 趋同）。
**最终 mutation**（`seaborn/utils.py`）：
```diff
-        formatter.set_useOffset(False)
         formatter.set_scientific(False)
```
**变异语义**：utils.py 里删除 `formatter.set_useOffset(False)`（只留 set_scientific）——relational 路径的 legend 仍带 offset，大数值标签错。删除 utils 侧禁用、覆盖 relational F2P。与 C（scales.py 改 pass）对称。重做为 D。

### Group E — 保留（E2 隐式→显式开关）
```diff
-            if hasattr(axis.major.formatter, "set_useOffset"):
-                axis.major.formatter.set_useOffset(False)
-            if hasattr(axis.major.formatter, "set_scientific"):
-                axis.major.formatter.set_scientific(False)
+            if globals().get('_DISABLE_OFFSET', False):
+                if hasattr(axis.major.formatter, "set_useOffset"):
+                    axis.major.formatter.set_useOffset(False)
+                if hasattr(axis.major.formatter, "set_scientific"):
+                    axis.major.formatter.set_scientific(False)
```
**变异语义**：scales.py 的 offset/scientific 禁用整体藏到模块级 `_DISABLE_OFFSET`（默认 False）开关后——默认不禁用 offset，objects legend 大数值标签带 offset 显示错。只有设 True 才禁用。默认即 bug。`test_plot` 失败。保留。

## 新设计 Mutation 说明

原始 A/C 作用于 scales.py，B/C/D 都围绕"禁用 set_useOffset"且多落在 scales.py，机制趋同且只覆盖 objects 路径。本次保留 A（scales.py useOffset True）、C（scales.py 改 pass）、E（scales.py _DISABLE_OFFSET 开关），重做 B、D 为 utils.py 路径（useOffset True / 删除调用），以同时覆盖 relational F2P 的另一文件。五组覆盖"scales useOffset True / utils useOffset True / scales 改 pass / utils 删除 / scales 默认开关"五个角度，跨 scales.py 与 utils.py 两文件、两接口路径——全部令大数值 legend 标签带 offset 显示错。全部实测（Python 3.9/seaborn 0.12.2.dev + matplotlib 3.5.3 + pandas 1.3.5，mpl34 环境）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
