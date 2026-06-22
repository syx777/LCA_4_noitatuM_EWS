# matplotlib__matplotlib-13989

## 问题背景

`hist()` 在 `density=True` 时不再尊重 `range=...` 参数。`plt.hist(data, "auto", range=(0,1), density=True)` 本应让 bins 的首值=0、末值=1，但实际 bins 落在数据范围而非 [0,1]。根因：`hist` 在计算完 bins 后，对 `density and not stacked` 的分支用 `hist_kwargs = dict(density=density)` **整体重建**了 `hist_kwargs` 字典——这覆盖丢弃了之前设置的 `hist_kwargs['range'] = bin_range`，导致 range 没传给底层 `np.histogram`。Golden patch 改成 `hist_kwargs['density'] = density`（原地更新键，保留 range）。

## Golden Patch 语义分析

```python
density = bool(density) or bool(normed)
if density and not stacked:
    hist_kwargs['density'] = density   # 原地更新，不丢 range
```
核心语义：**设置 density 时必须用 `hist_kwargs['density'] = density`（原地添加键）而非 `hist_kwargs = dict(density=density)`（重建字典）**——后者会丢弃此前在非自动-bins 分支设置的 `hist_kwargs['range'] = bin_range`。修复的关键是保留字典里已有的 range 键。

F2P 测试 `test_axes.py::test_hist_range_and_density`：`plt.hist(rand(10), "auto", range=(0,1), density=True)`，断言 `bins[0]==0` 且 `bins[-1]==1`。

## 调用链分析

`hist()` → 若非 list-of-bins 分支：`hist_kwargs['range'] = bin_range` → `density and not stacked` 时设 density → 每个数据集调 `np.histogram(..., **hist_kwargs)`。range 必须留在 hist_kwargs 里才能传给 np.histogram 约束 bin 边界。重建字典、clear、pop range、或上游加守卫不设 range，都会让 range 丢失、bins 不落在 [0,1]。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 还原原 bug：`dict(density=density)` 重建丢 range |
| B | ➕ 新增 | 新增 | `hist_kwargs.clear()` 后设 density，显式清空丢 range |
| C | ➕ 新增 | 新增 | 设 density 后 `pop('range')` 显式删 range 键 |
| D | 🟢 高质量 | 保留 | 上游 `hist_kwargs['range']` 加 `if not density` 守卫 |
| E | 🟢 高质量 | 保留 | 原地更新藏到 `_merge_hist_kwargs` 开关后 |

原始 A/C/E 字节相同（都是 `dict(density=density)`），A/B 又都加 range 守卫——实际只有"重建字典"和"range 守卫"两种机制。保留 A（重建）、D（range 守卫）、E（开关），新增 B（clear）、C（pop range）。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：重建字典）
```diff
         if density and not stacked:
-            hist_kwargs['density'] = density
+            hist_kwargs = dict(density=density)
```
**变异语义**：还原原始 bug——`hist_kwargs = dict(density=density)` 用新字典整体替换，丢弃之前设置的 `hist_kwargs['range']`。density=True 时 range 不再传给 np.histogram，bins 落在数据范围而非 [0,1]。F2P 失败。保留。

### Group B — 新增（D1 状态：clear 字典）
```diff
         if density and not stacked:
+            hist_kwargs.clear()
             hist_kwargs['density'] = density
```
**变异语义**：在设 density 前 `hist_kwargs.clear()` 显式清空整个字典，再设 density——同样丢弃 range 键。模拟"想重置字典、先 clear 再填、误删了 range"。与 A（dict 重建）效果相同但机制不同：保留了原变量、用 clear 清空。F2P 失败。新增为 B。

### Group C — 新增（D1 状态：pop range）
```diff
         if density and not stacked:
             hist_kwargs['density'] = density
+            hist_kwargs.pop('range', None)
```
**变异语义**：正确设了 density（用 golden 的原地更新），但紧接着 `hist_kwargs.pop('range', None)` 显式移除 range 键。density 模式下 range 被删，bins 不受约束。模拟"误把 range 当成与 density 冲突的多余键删掉"。比 A/B 隐蔽——density 设置是对的，错在多删了一个键。F2P 失败。新增为 C。

### Group D — 保留（B3 条件：上游 range 守卫）
```diff
         else:
-            hist_kwargs['range'] = bin_range
+            if not density:
+                hist_kwargs['range'] = bin_range
```
**变异语义**：在设置 range 的上游分支加 `if not density` 守卫——density 模式下根本不设 `hist_kwargs['range']`。从源头切断 range 传递（而非 A/B/C 那样先设后丢）。density=True 时 range 缺失，bins 不约束。F2P 失败。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
         if density and not stacked:
-            hist_kwargs['density'] = density
+            if getattr(self, '_merge_hist_kwargs', False):
+                hist_kwargs['density'] = density
+            else:
+                hist_kwargs = dict(density=density)
```
**变异语义**：原地更新 hist_kwargs 藏到 `_merge_hist_kwargs` 开关后（默认 False），默认走 `else` 的 `dict(density=density)` 重建丢 range，还原原 bug。只有显式开启才保留 range。模拟"把'保留已有 kwargs'做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

原始 A/C/E 字节完全相同（`dict(density=density)`），且 A/B 都加 range 守卫——实际只有"重建字典"和"range 守卫"两种机制。本次保留 A（dict 重建还原 bug）、D（上游 range 守卫）、E（_merge_hist_kwargs 默认关闭开关），新增 B（clear 清空字典）、C（pop 删 range 键）。五组覆盖"重建字典 / clear 清空 / pop 删键 / 上游守卫不设 / 默认关闭开关"五个角度——全部令 density 模式下 range 丢失、bins 不落在 [0,1]。全部实测（Python 3.9/matplotlib 3.0.2，源码构建 C 扩展）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
