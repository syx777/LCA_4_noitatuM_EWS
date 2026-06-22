# matplotlib__matplotlib-24177

## 问题背景

`ax.hist` 在 `histtype='step'`、`density=True` 时 density 轴未自动缩放以适配整个直方图。根因：`_AxesBase._update_patch_limits` 计算 patch 的 data limits 时，遍历 bezier 段用了 `p.iter_bezier()`（默认会做路径简化 simplify），把 step 直方图路径里小数值/近重合的顶点简化掉，autoscale 漏掉极值。Golden patch 改成 `p.iter_bezier(simplify=False)`——不简化，遍历全部顶点。

## Golden Patch 语义分析

```python
vertices = []
for curve, code in p.iter_bezier(simplify=False):
    _, dzeros = curve.axis_aligned_extrema()
    vertices.append(curve([0, *dzeros, 1]))
```
核心语义：**计算 patch data limits 时，`iter_bezier` 必须传 `simplify=False`，禁用路径简化，确保遍历到所有顶点（含小数值极值）**——否则简化会丢掉影响 autoscale 的顶点。关键点：显式 `simplify=False`（不依赖默认、不传 None 触发 rcParams、不在上游 cleaned 时简化）。

F2P 测试 `test_axes.py::test_small_autoscale`：构造含小数值的 Path，add_patch + autoscale，断言 xlim/ylim 覆盖所有顶点的 min/max。

## 调用链分析

`ax.add_patch` / autoscale → `_update_patch_limits(patch)` → `p.iter_bezier(simplify=False)` 遍历段 → 各段 extrema → vertices → 更新 data limits。若 simplify 被打开（True/None→rcParams/上游 cleaned），小数值顶点被简化丢弃，limits 不含全部顶点，autoscale 断言失败。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `simplify=False`→`simplify=True` |
| B | ➕ 补充 | 重做 | 删 simplify kwarg，默认简化 |
| C | ➕ 补充 | 重做 | `p.cleaned(simplify=True).iter_bezier(...)` 上游简化 |
| D | ➕ 补充 | 重做 | `simplify=None` 触发 rcParams 简化 |
| E | 🟢 高质量 | 保留 | simplify 藏到 simplify_bezier 开关后 |

原始 A==B、C==D（都 `iter_bezier()` 默认）。保留 A、E，重做 B（删 kwarg）、C（上游 cleaned 简化）、D（simplify=None）。

## 各组 Mutation 分析

### Group A — 保留（C1 值：simplify=True）
```diff
-        for curve, code in p.iter_bezier(simplify=False):
+        for curve, code in p.iter_bezier(simplify=True):
```
**变异语义**：`simplify=False` 改成 `simplify=True`——遍历 bezier 段时启用路径简化，step 直方图的小数值/近重合顶点被简化掉，autoscale 漏极值，data limits 不含全部顶点。还原原 bug。F2P 失败。保留。

### Group B — 重做（A2 接口契约：漏传 kwarg）
**原**：与 C/D 相同（`iter_bezier()`）。
**最终 mutation**：
```diff
-        for curve, code in p.iter_bezier(simplify=False):
+        for curve, code in p.iter_bezier():
```
**变异语义**：删除 `simplify=False` kwarg，`iter_bezier()` 用默认行为——按 rcParams['path.simplify']（默认 True）简化，等效启用简化，顶点丢失。漏传关键参数致默认简化生效。F2P 失败。重做为 B（与 A 的"显式 True"区分：这里是漏传致默认）。

### Group C — 重做（C1 值：上游 cleaned 简化）
**原**：与 D 相同（`iter_bezier()`）。
**最终 mutation**：
```diff
-        for curve, code in p.iter_bezier(simplify=False):
+        for curve, code in p.cleaned(simplify=True).iter_bezier(simplify=False):
```
**变异语义**：先 `p.cleaned(simplify=True)` 把路径简化成新路径，再 `iter_bezier(simplify=False)` 遍历——虽然 iter_bezier 不简化，但路径已在 cleaned 阶段被简化、顶点已丢。从路径清理阶段引入简化。比 A/B（在 iter_bezier 简化）更上游。F2P 失败。重做为 C。

### Group D — 重做（C1 值：simplify=None）
**原**：与 C 相同（`iter_bezier()`）。
**最终 mutation**：
```diff
-        for curve, code in p.iter_bezier(simplify=False):
+        for curve, code in p.iter_bezier(simplify=None):
```
**变异语义**：`simplify=False` 改成 `simplify=None`——None 让 iter_bezier 回退到 `rcParams['path.simplify']`（默认 True），等效启用简化。模拟"用 None 当'默认值'、却触发了 rcParams 的简化"。比 B（完全漏传）显式传了 None。F2P 失败。重做为 D。

### Group E — 保留（E2 隐式→显式开关）
```diff
-    def _update_patch_limits(self, patch):
+    def _update_patch_limits(self, patch, simplify_bezier=True):
...
-        for curve, code in p.iter_bezier(simplify=False):
+        for curve, code in p.iter_bezier(simplify=simplify_bezier):
```
**变异语义**：simplify 藏到 `_update_patch_limits(simplify_bezier=True)` 参数后（默认 True）。默认 `iter_bezier(simplify=True)`，autoscale 漏极值。调用方不传该参数 → 默认简化。模拟"把'不简化'做成可配置、默认却开了简化"。F2P 失败。保留。

## 新设计 Mutation 说明

原始 A==B（`simplify=True`）、C==D（`iter_bezier()` 默认），单行 patch 实际只有"开简化"一种机制。本次保留 A（显式 simplify=True）、E（simplify_bezier 默认开简化开关），重做 B（删 kwarg、默认简化）、C（上游 cleaned 简化）、D（simplify=None 触发 rcParams）。五组覆盖"显式 True / 漏传默认 / 上游 cleaned / None 触发 rcParams / 默认开关"五个角度——全部令路径简化生效、autoscale 漏掉小数值顶点。全部实测（Python 3.9/matplotlib 3.6.0，源码构建 C 扩展，conda 编译器）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
