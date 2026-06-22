# matplotlib__matplotlib-20676

## 问题背景

交互式 `SpanSelector` 错误地强制坐标轴范围包含 0。当 `ax.plot([10,20],[10,20])` 后创建 `SpanSelector(ax, print, "horizontal", interactive=True)` 时，x 轴范围被扩展到包含 x=0（应保持 (10,20)+margin）。根因：`_setup_edge_handle` 用 `self.extents` 初始化 `ToolLineHandles` 的位置，而 extents 默认含 0，把它当作初始手柄位置会撑大坐标轴。Golden patch 改成按方向取坐标轴当前 bound（`get_xbound()`/`get_ybound()`）作为初始位置，保持原有范围。

## Golden Patch 语义分析

```python
def _setup_edge_handle(self, props):
    # Define initial position using the axis bounds to keep the same bounds
    if self.direction == 'horizontal':
        positions = self.ax.get_xbound()
    else:
        positions = self.ax.get_ybound()
    self._edge_handles = ToolLineHandles(self.ax, positions, direction=self.direction, ...)
```
核心语义：**边缘手柄的初始位置必须取坐标轴当前 bound——水平方向用 `get_xbound()`、垂直方向用 `get_ybound()`——而非 `self.extents`（默认含 0）**。这样初始化不会改变坐标轴范围。关键点：按 direction 分支、水平↔xbound、垂直↔ybound 的正确配对，且不能用 extents。

F2P 测试 `test_widgets.py::test_span_selector_bound`（参数化 horizontal/vertical）：创建交互式 SpanSelector 后断言 `ax.get_xbound()`/`get_ybound()` 不变，且 `tool._edge_handles.positions == list(bound)`。

## 调用链分析

`SpanSelector.__init__(interactive=True)` → `new_axes` → `_setup_edge_handle(props)` → 取 positions → `ToolLineHandles(self.ax, positions, direction=...)`。positions 若为 extents（含 0）则 ToolLineHandles 绘制的初始线撑大坐标轴。direction 分支错配、用 extents、或藏到开关后，都会让坐标轴范围被错误扩展或手柄位置取错轴。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 还原原 bug：`positions = self.extents` |
| B | 🟢 高质量 | 保留 | 方向判断 `== 'horizontal'`→`!= 'horizontal'` |
| C | ➕ 新增 | 新增 | else 分支也用 `get_xbound()`（垂直取错轴） |
| D | ➕ 新增 | 新增 | 两分支 xbound/ybound 整体对调 |
| E | 🟢 高质量 | 保留 | 按轴 bound 初始化藏到 `_init_from_bounds` 开关后 |

原始 A/C/D 字节相同（都是 `positions = self.extents`）。保留 A（extents 还原 bug）、B（方向反转）、E（开关），新增 C（else 用 xbound）、D（两分支对调）。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：用 extents）
```diff
-        # Define initial position using the axis bounds to keep the same bounds
-        if self.direction == 'horizontal':
-            positions = self.ax.get_xbound()
-        else:
-            positions = self.ax.get_ybound()
+        positions = self.extents
```
**变异语义**：还原原始 bug——用 `self.extents` 初始化手柄位置。extents 默认含 0，ToolLineHandles 据此绘制的初始线把坐标轴范围撑大到包含 0。F2P 断言 bound 不变，实际被扩展 → 失败。保留。

### Group B — 保留（B3 条件反转：方向取反）
```diff
-        if self.direction == 'horizontal':
+        if self.direction != 'horizontal':
             positions = self.ax.get_xbound()
         else:
             positions = self.ax.get_ybound()
```
**变异语义**：方向判断 `== 'horizontal'` 反转成 `!= 'horizontal'`。水平方向走 else 取 `get_ybound()`、垂直方向取 `get_xbound()`——手柄初始位置取了与方向不匹配的坐标轴 bound。F2P 断言 `positions == list(bound)`（对应方向）失败。保留。

### Group C — 新增（D4 状态：else 取错轴）
```diff
         if self.direction == 'horizontal':
             positions = self.ax.get_xbound()
         else:
-            positions = self.ax.get_ybound()
+            positions = self.ax.get_xbound()
```
**变异语义**：else 分支（垂直方向）也用 `get_xbound()` 而非 `get_ybound()`。水平方向仍正确，但垂直 SpanSelector 的手柄位置取了 x 轴 bound——方向与坐标轴不匹配。比 B（改 if 条件）只影响垂直分支，更局部。F2P 的 vertical 参数化失败。新增为 C。

### Group D — 新增（D4 状态：两分支对调）
```diff
         if self.direction == 'horizontal':
-            positions = self.ax.get_xbound()
+            positions = self.ax.get_ybound()
         else:
-            positions = self.ax.get_ybound()
+            positions = self.ax.get_xbound()
```
**变异语义**：两个分支的 `get_xbound()`/`get_ybound()` 整体对调——水平取 ybound、垂直取 xbound，方向与坐标轴系统性错配。与 B（反转 if 条件）效果相同但机制不同：if 条件不变、直接交换了分支体。比 B 隐蔽——条件看着是对的。F2P horizontal+vertical 都失败。新增为 D。

### Group E — 保留（E2 隐式→显式开关）
```diff
-        if self.direction == 'horizontal':
-            positions = self.ax.get_xbound()
-        else:
-            positions = self.ax.get_ybound()
+        if getattr(self, '_init_from_bounds', False):
+            if self.direction == 'horizontal':
+                positions = self.ax.get_xbound()
+            else:
+                positions = self.ax.get_ybound()
+        else:
+            positions = self.extents
```
**变异语义**：按轴 bound 初始化藏到 `_init_from_bounds` 开关后（默认 False），默认走 `positions = self.extents`（含 0），还原原 bug 的范围扩展。只有显式开启才用 bound。模拟"把'用轴 bound 初始化手柄'做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

原始 A/C/D 字节完全相同（`positions = self.extents`），只有"用 extents"和"方向反转"两种机制。本次保留 A（extents 还原 bug）、B（方向条件反转）、E（_init_from_bounds 默认关闭开关），新增 C（else 分支取错轴 xbound）、D（两分支 bound 整体对调）。五组覆盖"用 extents / 方向条件反转 / 单分支取错轴 / 双分支对调 / 默认关闭开关"五个角度——全部令手柄初始位置错误、坐标轴范围被改或取错轴。全部实测（Python 3.9/matplotlib 3.4.2，源码构建 C 扩展）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
