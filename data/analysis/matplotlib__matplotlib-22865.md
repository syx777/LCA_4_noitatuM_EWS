# matplotlib__matplotlib-22865

## 问题背景

`Colorbar` 在 `drawedges=True` 且 `extend='both'`（或 min/max）时，两端 extremity 的分隔线（黑线）不绘制。`drawedges` 本应在颜色块之间画黑线分隔，但延伸端（extend 区域）的分隔线缺失。根因：`_add_solids` 里 `self.dividers.set_segments` 用 `np.dstack([X, Y])[1:-1]` 硬编码去掉首尾两段——这恰好把 extend 端的分隔线也去掉了。Golden patch 根据 `_extend_lower()`/`_extend_upper()` 动态决定起止索引：延伸端存在时保留该端分隔线（start_idx=0 / end_idx=len(X)），否则去掉（start_idx=1 / end_idx=-1）。

## Golden Patch 语义分析

```python
if self.drawedges:
    start_idx = 0 if self._extend_lower() else 1
    end_idx = len(X) if self._extend_upper() else -1
    self.dividers.set_segments(np.dstack([X, Y])[start_idx:end_idx])
else:
    self.dividers.set_segments([])
```
核心语义：**分隔线段的切片起止由 extend 状态决定——下端延伸（`_extend_lower()`）时 start_idx=0（含首段）否则 1；上端延伸（`_extend_upper()`）时 end_idx=len(X)（含末段）否则 -1**。这样延伸端的 extremity 分隔线被保留。关键点：start_idx 的 0/1 三元、end_idx 的 len(X)/-1 三元、以及它们与 extend_lower/upper 的正确配对。

F2P 测试 `test_colorbar.py::test_colorbar_extend_drawedges`（参数化 both/min/max/neither）：断言 `cbar.dividers.get_segments()` 等于预期的分隔线段数组（含延伸端）。

## 调用链分析

`Colorbar._add_solids(X, Y, C)` → `drawedges` 时计算 start_idx/end_idx → `self.dividers.set_segments(np.dstack([X,Y])[start_idx:end_idx])`。切片索引决定哪些段被画成分隔线。start_idx/end_idx 的三元分支反转、边界值改、删 else、或写死，都会让 extend 端的分隔线多画/少画/索引错。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | start/end 两个三元分支整体对调 |
| B | 🟢 高质量 | 保留 | `end_idx = len(X)`→`len(X) - 1`，上端少一段 |
| C | 🟢 高质量 | 保留 | 非 extend 时 `end_idx = -1`→`None`，多取末段 |
| D | 🟢 高质量 | 保留 | start_idx 三元改成无 else 的 if，未定义/残值 |
| E | 🟢 高质量 | 保留 | start_idx 写死成 1，下端 extremity 漏画 |

五组机制各异（分支对调 / 边界 -1 / None 切片 / 删 else / 写死），全部保留并核验。

## 各组 Mutation 分析

### Group A — 保留（B3 条件反转：分支对调）
```diff
-            start_idx = 0 if self._extend_lower() else 1
-            end_idx = len(X) if self._extend_upper() else -1
+            start_idx = 1 if self._extend_lower() else 0
+            end_idx = -1 if self._extend_upper() else len(X)
```
**变异语义**：start_idx 和 end_idx 的三元真假分支整体对调（0↔1、len(X)↔-1）。extend 端该保留分隔线时反而去掉、不该保留时反而保留——drawedges+extend 的分隔线段集合与期望完全相反。F2P 各参数化用例失败。保留。

### Group B — 保留（C1 值：上端少一段）
```diff
-            end_idx = len(X) if self._extend_upper() else -1
+            end_idx = len(X) - 1 if self._extend_upper() else -1
```
**变异语义**：上端延伸时 `end_idx = len(X)` 改成 `len(X) - 1`。切片少取最后一段——extend='both'/'max' 时上端 extremity 的分隔线缺失，segments 数组比期望少一项。模拟"边界索引差一（off-by-one）"。F2P 的 both/max 用例失败。保留。

### Group C — 保留（C1 值：None 切片多取）
```diff
-            end_idx = len(X) if self._extend_upper() else -1
+            end_idx = len(X) if self._extend_upper() else None
```
**变异语义**：非上端延伸时 `end_idx = -1` 改成 `None`。切片 `[start:None]` 取到末尾而非倒数第一——neither/min 情况下多取一段末端分隔线，segments 比期望多一项。模拟"用 None 代替 -1、切片语义不同（None=到末尾）"。F2P 的 neither/min 失败。保留。

### Group D — 保留（B2 删 else 分支）
```diff
-            start_idx = 0 if self._extend_lower() else 1
+            if self._extend_lower():
+                start_idx = 0
```
**变异语义**：把 `start_idx = 0 if ... else 1` 三元改成只有 if 分支的语句——非 extend_lower 时 `start_idx` 未被赋值，沿用上一轮循环的残值或触发 `NameError`（首次）。下端分隔线索引错误或崩溃。模拟"重构三元为 if、漏写 else"。F2P 失败。保留。

### Group E — 保留（C1 值：写死 start_idx）
```diff
-            start_idx = 0 if self._extend_lower() else 1
+            start_idx = 1
```
**变异语义**：`start_idx = 0 if self._extend_lower() else 1` 写死成 `start_idx = 1`，无视 extend_lower。下端延伸（extend='both'/'min'）时本应 start_idx=0 保留首段分隔线，现恒为 1 → 下端 extremity 分隔线被漏画。F2P 的 both/min 失败。模拟"把条件取值写成了固定常量"。保留。

## 新设计 Mutation 说明

本实例原五组机制已各不相同且全部有效，无重复，故全部保留并逐一核验。五组覆盖"分支整体对调 / 上端边界 off-by-one / None 切片多取 / 删 else 致未定义 / 写死 start_idx"五个角度，分别作用于三元分支、end_idx 上界、end_idx 下界切片、start_idx 控制流、start_idx 取值五个环节——全部令 drawedges+extend 的分隔线段与期望不符。全部实测（Python 3.9/matplotlib 3.5.0，源码构建 C 扩展）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
