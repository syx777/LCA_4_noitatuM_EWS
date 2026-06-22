# matplotlib__matplotlib-14623

## 问题背景

用 limits 反转 log 轴失效（3.1.0 起）。`ax.set_ylim(y.max(), y.min())`（即 set_ylim(大, 小)）对 linear 轴能反转 y 轴，对 log 轴却不行。根因：`set_ylim` 调用 `nonsingular` 和 `limit_range_for_scale` 时，log 的 `LogLocator.nonsingular` 用 `increasing=False` 等逻辑把 bottom/top 重新正序化，丢失了用户传入的倒序意图。Golden patch 在调这些方法**之前**记录 `swapped = bottom > top`，调用后若 `swapped` 则把结果交换回倒序；同时 `LogLocator.nonsingular` 去掉 `increasing=False`。

## Golden Patch 语义分析

```python
swapped = bottom > top
bottom, top = self.yaxis.get_major_locator().nonsingular(bottom, top)
bottom, top = self.yaxis.limit_range_for_scale(bottom, top)
if swapped:
    bottom, top = top, bottom
self.viewLim.intervaly = (bottom, top)
```
核心语义：**在 `nonsingular`/`limit_range_for_scale`（可能把范围正序化）**之前**捕获用户是否传入倒序（`swapped = bottom > top`），调用后若 swapped 则交换回 `top, bottom` 恢复倒序意图**。关键点：swapped 必须在正序化**之前**捕获（之后捕获恒为 False）、比较是 `>`、恢复是无条件交换 `top, bottom`、`if swapped` 守卫为肯定式。

F2P 测试 `test_axes.py::test_inverted_limits`：新增对 log 轴 `set_ylim(10, 1)` 断言 `get_ylim() == (10, 1)`（倒序保持）。

## 调用链分析

`set_ylim(bottom, top)` → `swapped = bottom > top`（捕获倒序）→ `nonsingular` + `limit_range_for_scale`（正序化）→ `if swapped: bottom, top = top, bottom`（恢复）→ `viewLim.intervaly`。swapped 比较反转、捕获时机错、恢复无效/取反/禁用，都会让 log 轴倒序丢失（得到正序 (1,10)）。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 新增 | 新增 | `swapped = bottom > top`→`< top`，倒序判断反转 |
| B | ➕ 新增 | 新增 | 恢复守卫 `if swapped`→`if not swapped` |
| C | ➕ 新增 | 新增 | 恢复语句改成无操作 `bottom, top = bottom, top` |
| D | 🟢 高质量 | 保留 | swapped 捕获移到正序化之后，恒 False |
| E | 🟢 高质量 | 保留 | 恢复 `if swapped`→`if False: # swapped` |

原始仅 D、E。新增 A（比较反转）、B（守卫反转）、C（恢复无操作）。F2P 只测 set_ylim log，故全部针对 `_base.py set_ylim` 区域。

## 各组 Mutation 分析

### Group A — 新增（B3 条件反转：比较反转）
```diff
-        swapped = bottom > top
+        swapped = bottom < top
```
**变异语义**：倒序判断 `bottom > top` 反转成 `bottom < top`。用户传 set_ylim(10,1)（bottom=10>top=1）本应 swapped=True，现 `10<1` 为 False → 不恢复倒序 → log 轴得到正序化后的 (1,10)。倒序意图被误判。F2P 断言 (10,1) 失败。新增为 A。

### Group B — 新增（B3 条件反转：守卫反转）
```diff
-        if swapped:
+        if not swapped:
             bottom, top = top, bottom
```
**变异语义**：恢复守卫 `if swapped` 取反成 `if not swapped`。该恢复倒序时（swapped=True）不恢复、不该恢复时（正序输入）反而交换。log 轴 set_ylim(10,1) 的 swapped=True → `not` 为假 → 不恢复 → 正序。F2P 失败。新增为 B。

### Group C — 新增（D1 状态：恢复无操作）
```diff
         if swapped:
-            bottom, top = top, bottom
+            bottom, top = bottom, top
```
**变异语义**：恢复语句 `bottom, top = top, bottom`（交换）写成 `bottom, top = bottom, top`（赋值自身、无操作）。swapped 正确检测到，但实际未交换回倒序——log 轴经 limit_range_for_scale 正序化后不再恢复。模拟"交换语句左右写反致无操作"。F2P 失败。新增为 C。

### Group D — 保留（D1 状态：捕获时机错）
```diff
-        swapped = bottom > top
         bottom, top = self.yaxis.get_major_locator().nonsingular(bottom, top)
         bottom, top = self.yaxis.limit_range_for_scale(bottom, top)
+        swapped = bottom > top
         if swapped:
```
**变异语义**：`swapped = bottom > top` 的捕获从正序化**之前**移到**之后**。此时 bottom/top 已被 `nonsingular`/`limit_range_for_scale` 正序化（bottom<top），`swapped` 恒为 False → 永不恢复倒序。捕获时机错使倒序信息丢失。F2P 失败。保留。

### Group E — 保留（E2 隐式→显式：禁用恢复）
```diff
         bottom, top = self.yaxis.limit_range_for_scale(bottom, top)
-        if swapped:
+        if False:  # swapped:
             bottom, top = top, bottom
```
**变异语义**：恢复倒序的 `if swapped` 改成 `if False:  # swapped`——倒序恢复被永久禁用。无论用户是否传倒序，结果都不恢复，log 轴 set_ylim(10,1) 得到正序。模拟"把恢复逻辑注释/禁用（开关恒关）"。F2P 失败。保留。

## 新设计 Mutation 说明

原始仅 D、E 两组（捕获时机错、禁用恢复）。本次保留 D（swapped 捕获移到正序化后）、E（`if False` 禁用恢复），新增 A（`>`→`<` 比较反转）、B（`if swapped`→`if not swapped` 守卫反转）、C（恢复语句改无操作 `bottom, top = bottom, top`）。五组覆盖"比较反转 / 守卫反转 / 恢复无操作 / 捕获时机错 / 禁用恢复"五个角度，全部作用于 `_base.py set_ylim` 的倒序保持逻辑（F2P 只测 log set_ylim）——全部令 log 轴倒序意图丢失。全部实测（Python 3.9/matplotlib 3.1.0，源码构建 C 扩展）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
