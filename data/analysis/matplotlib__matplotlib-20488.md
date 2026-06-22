# matplotlib__matplotlib-20488

## 问题背景

`test_huge_range_log` 测试失败（CI 偶发 ValueError）。`_ImageBase._make_image` 在用 `LogNorm` 归一化重采样后的图像数据时，会把重新计算的 `vmin/vmax`（`vrange`）传给 norm。当数据含 `-1` 这类负值、且 `LogNorm` 的 `s_vmin` 落到 0 或负值时，`LogNorm` 内部 `log(s_vmin)` 得到 `-inf`，`transform` 后 vmin 非有限，`LogNorm.__call__` 抛 `ValueError("Invalid vmin or vmax")`。原代码只在 `s_vmin < 0` 时钳制到 eps，漏了 `s_vmin == 0`。Golden patch 把条件改为 `s_vmin <= 0` 并统一钳制到 `np.finfo(scaled_dtype).eps`。

## Golden Patch 语义分析

```python
s_vmin, s_vmax = vrange
if isinstance(self.norm, mcolors.LogNorm) and s_vmin <= 0:
    # Don't give 0 or negative values to LogNorm
    s_vmin = np.finfo(scaled_dtype).eps
with cbook._setattr_cm(self.norm, vmin=s_vmin, vmax=s_vmax):
    output = self.norm(resampled_masked)
```
核心语义：**仅当 norm 是 LogNorm 且 `s_vmin <= 0`（含 0）时，把 s_vmin 钳制到该 dtype 的最小正数 eps**——0 和负数对 LogNorm 都是非法的（log 无定义），必须替换成一个极小正值。关键点：条件含等于（`<= 0` 而非 `< 0`）、钳制目标是 eps（正数）、被钳制的是 s_vmin（不是 s_vmax）。

F2P 测试 `test_image.py::test_huge_range_log`（参数化 x∈{-1,1}）：对含负值和大范围（1→1e20）的数据用 LogNorm 渲染，断言与 Normalize 参考图一致、不抛 ValueError。

## 调用链分析

`_ImageBase.make_image` → `_make_image(A, ...)` → 重采样得 `A_resampled` → `s_vmin, s_vmax = vrange` → LogNorm 钳制 → `cbook._setattr_cm(self.norm, vmin=s_vmin, ...)` → `self.norm(resampled_masked)` → `LogNorm.__call__` 内 `transform([vmin, vmax])` 须有限。s_vmin 为 0/负 → log 非有限 → ValueError。钳制条件边界、目标值、目标变量、或开关默认值出错都会重新触发崩溃。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `s_vmin <= 0`→`< 0`，漏掉 s_vmin==0 边界 |
| B | 🟢 高质量 | 重做 | `s_vmin <= 0`→`> 0`，钳制条件反转 |
| C | 🟢 高质量 | 保留 | 钳制目标 `eps`→`0`，0 对 LogNorm 仍非法 |
| D | ➕ 新增 | 新增 | 误钳制 `s_vmax` 而非 `s_vmin` |
| E | 🟢 高质量 | 保留 | 钳制藏到 `_clip_lognorm_vmin` 开关后 |

原始 A/B/D 字节相同（都是 `<= 0`→`< 0`）。保留 A（边界遗漏）、C（目标值 0）、E（开关），重做 B（条件反转 `> 0`）、新增 D（钳错变量 s_vmax）。

## 各组 Mutation 分析

### Group A — 保留（B3 边界：漏 0 边界）
```diff
-                if isinstance(self.norm, mcolors.LogNorm) and s_vmin <= 0:
+                if isinstance(self.norm, mcolors.LogNorm) and s_vmin < 0:
```
**变异语义**：钳制条件 `s_vmin <= 0` 改成 `< 0`，漏掉 `s_vmin == 0` 的情况。当 s_vmin 恰为 0 时不钳制，0 直接传给 LogNorm → `log(0) = -inf` → transform 后 vmin 非有限 → `ValueError("Invalid vmin or vmax")`。这正是原始 bug（边界遗漏）。F2P 失败。保留。

### Group B — 重做（B3 条件反转：> 0）
**原**：与 A/D 相同（`<= 0`→`< 0`）。
**最终 mutation**：
```diff
-                if isinstance(self.norm, mcolors.LogNorm) and s_vmin <= 0:
+                if isinstance(self.norm, mcolors.LogNorm) and s_vmin > 0:
```
**变异语义**：钳制条件 `s_vmin <= 0` 反转成 `s_vmin > 0`。语义完全颠倒——只有正 s_vmin（本来合法、无需钳制）才被钳制到 eps，而真正非法的 0/负值反而不处理、直接进 LogNorm 崩溃。比 A（少一个等号）更彻底地破坏条件。F2P 失败。重做为 B。

### Group C — 保留（C1 值：钳到 0）
```diff
                 if isinstance(self.norm, mcolors.LogNorm) and s_vmin <= 0:
                     # Don't give 0 or negative values to LogNorm
-                    s_vmin = np.finfo(scaled_dtype).eps
+                    s_vmin = 0
```
**变异语义**：钳制目标从 `np.finfo(scaled_dtype).eps`（极小正数）改成 `0`。条件命中了（s_vmin<=0 时进入），但把 s_vmin 设成 0 对 LogNorm 仍是非法值——`log(0) = -inf`。钳制形同无效。模拟"用 0 兜底负值、却忘了 0 对对数同样非法"。F2P 失败。保留。

### Group D — 新增（D4 状态：钳错变量）
```diff
                 if isinstance(self.norm, mcolors.LogNorm) and s_vmin <= 0:
                     # Don't give 0 or negative values to LogNorm
-                    s_vmin = np.finfo(scaled_dtype).eps
+                    s_vmax = np.finfo(scaled_dtype).eps
```
**变异语义**：钳制时误把 `s_vmin = eps` 写成 `s_vmax = eps`——s_vmin（非法的 0/负值）未被修正，仍传给 LogNorm 崩溃；同时 s_vmax 被错误地改成极小值。模拟"赋值左侧变量名写错（vmin/vmax 混淆）"。比 C（钳到错值）隐蔽——目标值对、改错了变量。F2P 失败。新增为 D。

### Group E — 保留（E2 隐式→显式开关）
```diff
-                if isinstance(self.norm, mcolors.LogNorm) and s_vmin <= 0:
+                if getattr(self, '_clip_lognorm_vmin', False) and isinstance(self.norm, mcolors.LogNorm) and s_vmin <= 0:
```
**变异语义**：钳制条件前置 `getattr(self, '_clip_lognorm_vmin', False)` 开关（默认不存在 → False）。默认情况下永不钳制 → 0/负 s_vmin 直接进 LogNorm 崩溃，还原原 bug。只有显式设该属性才钳制。模拟"把 LogNorm vmin 钳制做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

原始 A/B/D 字节完全相同（`s_vmin <= 0`→`< 0`），只有"边界遗漏 + 钳到 0 + 开关"三种机制。本次保留 A（漏 0 边界）、C（钳到 0 仍非法）、E（_clip_lognorm_vmin 默认关闭开关），重做 B（条件反转 `> 0`）、新增 D（误钳 s_vmax）。五组覆盖"漏等号边界 / 条件反转 / 钳到非法值 / 钳错变量 / 默认关闭开关"五个角度——全部令 0/负 vmin 传入 LogNorm 触发 ValueError。全部实测（Python 3.9/matplotlib 3.4.2，源码构建 C 扩展）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
