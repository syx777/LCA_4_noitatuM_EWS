# matplotlib__matplotlib-25960

## 问题背景

`Figure.subfigures` 的 `wspace`/`hspace` 参数不起作用——无论取值，subfigure 间距不变。根因：GridSpec 的 wspace/hspace 在 subfigure 实例化时被忽略，且未在事后补偿。Golden patch 两处：(1) 给 GridSpec 加 `left=0, right=1, bottom=0, top=1`（铺满）；(2) 在创建 subfigure 后，若无 layout engine 且设了 wspace/hspace，则用 `gs.get_grid_positions` 算出各 subfigure 的 bbox 并 `_redo_transform_rel_fig` 重新定位。

## Golden Patch 语义分析

```python
gs = GridSpec(..., width_ratios=..., height_ratios=...,
              left=0, right=1, bottom=0, top=1)
...
if self.get_layout_engine() is None and (wspace is not None or hspace is not None):
    bottoms, tops, lefts, rights = gs.get_grid_positions(self)
    for sfrow, bottom, top in zip(sfarr, bottoms, tops):
        for sf, left, right in zip(sfrow, lefts, rights):
            bbox = Bbox.from_extents(left, bottom, right, top)
            sf._redo_transform_rel_fig(bbox=bbox)
```
核心语义：**(1) GridSpec 须铺满 figure（left/right/bottom/top=0/1/0/1）；(2) 无 layout engine 且设了 wspace 或 hspace 时，按 grid 位置重定位各 subfigure 的 bbox**。关键点：GridSpec 的四个边界参数、补偿条件 `is None and (... or ...)`、bbox 重定位循环。

F2P 测试 `test_figure.py::test_subfigures_wspace_hspace`：2x3 subfigures + hspace/wspace，断言各 subfigure 的 bbox.min/max 精确值。

## 调用链分析

`fig.subfigures(2,3,hspace=.5,wspace=1/6)` → GridSpec(铺满) → 创建 subfigures → `if no engine and (wspace/hspace set)` → grid 位置 → 重定位 bbox。GridSpec 边界、补偿条件、bbox 重定位任一出错，subfigure 位置不符断言。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `is None`→`is not None`，补偿条件反转 |
| B | 🟢 高质量 | 重做 | `or`→并把 not None 改 None，补偿不触发 |
| C | ➕ 补充 | 重做 | GridSpec `top=1`→`top=0.999`，边界偏移 |
| D | 🟢 高质量 | 保留 | 删 GridSpec left/right/bottom/top（还原 bug） |
| E | 🟢 高质量 | 保留 | 补偿藏到 use_gridspec_spacing 开关后 |

原始 A==B==C（`is None`→`is not None`）。保留 A、D、E，重做 B（`is None or`→`is None`，且 or→... 使条件不触发）、C（GridSpec top 边界偏移）。

## 各组 Mutation 分析

### Group A — 保留（B3 条件反转）
```diff
-        if self.get_layout_engine() is None and (wspace is not None or
+        if self.get_layout_engine() is not None and (wspace is not None or
                                                  hspace is not None):
```
**变异语义**：`get_layout_engine() is None` 反转成 `is not None`——无 layout engine（默认）时不进入 spacing 补偿分支，wspace/hspace 被忽略（原 bug）；有 engine 时反而进入。F2P（默认无 engine）失败。保留。

### Group B — 重做（B3 条件：wspace/hspace 判定反转）
**原**：与 A 相同（`is None`→`is not None`）。
**最终 mutation**：
```diff
-        if self.get_layout_engine() is None and (wspace is not None or
-                                                 hspace is not None):
+        if self.get_layout_engine() is None and (wspace is None or
+                                                 hspace is None):
```
**变异语义**：把 `wspace is not None or hspace is not None`（任一设置）反转成 `wspace is None or hspace is None`（任一未设置）。F2P 同时设了 wspace 和 hspace（都非 None）→ `None or None` 为假 → 不补偿，间距被忽略。条件判定方向反转（与 A 的 engine 判定反转不同环节）。F2P 失败。重做为 B。

### Group C — 重做（C1 值：GridSpec 边界偏移）
**原**：与 A 相同。
**最终 mutation**：
```diff
                       height_ratios=height_ratios,
-                      left=0, right=1, bottom=0, top=1)
+                      left=0, right=1, bottom=0, top=0.999)
```
**变异语义**：GridSpec 的 `top=1` 改成 `top=0.999`——子图网格顶边界微偏，各 subfigure 的 bbox 顶部位置与期望（精确 h、h*0.6 等）不符。F2P 的 `assert_allclose` 失败。模拟"边界常量写错一点点"。重做为 C（作用于 GridSpec 边界、与 A/B 的条件不同）。

### Group D — 保留（D1 状态：删 GridSpec 边界）
```diff
                       height_ratios=height_ratios,
-                      left=0, right=1, bottom=0, top=1)
+                      )
```
**变异语义**：删除 GridSpec 的 `left=0, right=1, bottom=0, top=1` 参数——还原原 bug：subfigure 网格不铺满 figure（用默认带边距的布局），bbox 位置错，且 wspace/hspace 补偿基于错误的网格位置。F2P 失败。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
                    width_ratios=None, height_ratios=None,
+                   use_gridspec_spacing=False,
                    **kwargs):
...
-        if self.get_layout_engine() is None and (wspace is not None or
+        if use_gridspec_spacing and self.get_layout_engine() is None and (wspace is not None or
```
**变异语义**：spacing 补偿藏到 `subfigures(use_gridspec_spacing=False)` 参数后（默认 False）——默认不做 wspace/hspace 补偿（原 bug）。只有显式开启才生效。模拟"把间距补偿做成可配置、默认却关掉"。F2P 失败。保留。

## 新设计 Mutation 说明

原始 A==B==C 字节相同（都 `is None`→`is not None`），实际只有"条件反转 / 删 GridSpec 边界 / 开关"三种机制。本次保留 A（engine 判定反转）、D（删 GridSpec 边界还原 bug）、E（use_gridspec_spacing 默认关闭开关），重做 B（wspace/hspace 判定 `not None or`→`None or` 反转、补偿不触发）、C（GridSpec `top=0.999` 边界偏移）。五组覆盖"engine 判定反转 / wspace 判定反转 / 边界偏移 / 删边界 / 默认关闭开关"五个角度，作用于补偿条件的两部分、GridSpec 边界、特性开关——全部令 subfigure 间距/位置不符。全部实测（Python 3.9/matplotlib 3.7.1，源码构建 C 扩展，conda 编译器）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
