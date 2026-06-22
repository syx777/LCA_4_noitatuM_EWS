# matplotlib__matplotlib-24570

## 问题背景

`HPacker` 的 `align` 参数中 `top` 和 `bottom` 反了。根因：`_get_aligned_offsets` 把 align 分组成 `["left", "top"]`（用 `offsets=[d...]`）和 `["right", "bottom"]`（用 `offsets=[height-h+d...]`），但 top/bottom 的偏移算法被错配。Golden patch 把分组改成 `["left", "bottom"]` 和 `["right", "top"]`——即把 top 与 bottom 在两个分支间互换，修正对齐方向。

## Golden Patch 语义分析

```python
elif align in ["left", "bottom"]:
    descent = 0.
    offsets = [d for h, d in hd_list]
elif align in ["right", "top"]:
    descent = 0.
    offsets = [height - h + d for h, d in hd_list]
```
核心语义：**`bottom` 应与 `left` 同组（用 `offsets=[d...]`），`top` 应与 `right` 同组（用 `offsets=[height-h+d...]`）**——原 bug 把 top/bottom 放反了组。关键点：两个分支的 align 列表里 top↔bottom 的正确归属，决定偏移算法。

F2P 测试 `test_offsetbox.py::test_packers`（参数化 baseline/bottom/top/left/right/center）：对 HPacker/VPacker 断言各 align 的 offset_pairs。其中 bottom 应得 `y_height=0`（同 left/baseline），top 应得 `y_height=y2-y1`（同 right）。

## 调用链分析

`HPacker.get_extent_offsets` → `_get_aligned_offsets(hd_list, height, align)` → 按 align 分组选 offsets 公式。若 top/bottom 归错组、或某分支公式错、或门控开关，则 top/bottom 对齐位置错，F2P 的 offset_pairs 断言失败。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 补充 | 新增 | 只改第一分支 `bottom`→`top`，bottom 失配 |
| B | 🟢 高质量 | 保留 | left/bottom 分支公式换成 top 的公式 |
| C | 🟢 高质量 | 重做 | 两分支整体换回原 bug 分组 |
| D | ➕ 补充 | 新增 | 只改第二分支 `top`→`bottom`，top 失配 |
| E | 🟢 高质量 | 保留 | 分组藏到 fix_align 开关后 |

原始 C==D==E（都整体换回原 bug），缺 A。保留 B、E，重做 C（整体换回），补充 A（只改第一分支）、D（只改第二分支）。

## 各组 Mutation 分析

### Group A — 新增（B2 部分还原：改第一分支）
```diff
-    elif align in ["left", "bottom"]:
+    elif align in ["left", "top"]:
```
**变异语义**：只把第一个分支 `["left", "bottom"]` 改回 `["left", "top"]`——`top` 被错误地用 `offsets=[d...]`（本应是 bottom 的算法），而 `bottom` 落到第二分支（`["right", "top"]` 不含 bottom）或 center/未匹配。top 对齐位置错。部分还原 bug。F2P 的 top 子用例失败。新增为 A。

### Group B — 保留（C1 值：公式替换）
```diff
     elif align in ["left", "bottom"]:
         descent = 0.
-        offsets = [d for h, d in hd_list]
+        offsets = [height - h + d for h, d in hd_list]
```
**变异语义**：left/bottom 分支的 `offsets=[d...]` 替换成 `[height-h+d...]`（top/right 分支的公式）。bottom/left 对齐用了 top 的偏移算法，元素位置错。改公式而非改分组——分组对、算法错。F2P 失败。保留。

### Group C — 重做（B3 整体换回 bug）
**原**：与 D/E 相同（整体换回原 bug 分组）。
**最终 mutation**：
```diff
-    elif align in ["left", "bottom"]:
+    elif align in ["left", "top"]:
         offsets = [d for h, d in hd_list]
-    elif align in ["right", "top"]:
+    elif align in ["right", "bottom"]:
         offsets = [height - h + d for h, d in hd_list]
```
**变异语义**：两个分支整体换回原始 bug 的 `["left","top"]`/`["right","bottom"]`——top/bottom 对齐完全反转，还原原始 align 颠倒 bug。F2P 的 top 和 bottom 子用例都失败。重做为 C（完整还原 bug，与 A/D 的部分改动区分）。

### Group D — 新增（B2 部分还原：改第二分支）
```diff
-    elif align in ["right", "top"]:
+    elif align in ["right", "bottom"]:
```
**变异语义**：只把第二个分支 `["right", "top"]` 改回 `["right", "bottom"]`——`bottom` 被错误地用 `offsets=[height-h+d...]`（本应是 top 的算法），而 `top` 落到第一分支不含/未匹配。bottom 对齐位置错。与 A 对称（A 改第一分支影响 top、D 改第二分支影响 bottom）。F2P 的 bottom 子用例失败。新增为 D。

### Group E — 保留（E2 隐式→显式开关）
```diff
-def _get_aligned_offsets(hd_list, height, align="baseline"):
+def _get_aligned_offsets(hd_list, height, align="baseline", fix_align=False):
...
-    elif align in ["left", "bottom"]:
+    elif align in (["left", "bottom"] if fix_align else ["left", "top"]):
-    elif align in ["right", "top"]:
+    elif align in (["right", "top"] if fix_align else ["right", "bottom"]):
```
**变异语义**：align 分组藏到 `fix_align` 开关后（默认 False）。默认走原 bug 的 `["left","top"]`/`["right","bottom"]` 分组，top/bottom 颠倒。只有显式开启才用修正分组。模拟"把 align 修正做成可配置、默认却关掉"。F2P 失败。保留。

## 新设计 Mutation 说明

原始 C==D==E 字节相同（都整体换回原 bug 分组），缺 A。本次保留 B（公式替换）、E（fix_align 默认关闭开关），重做 C（整体换回原 bug、完整还原），补充 A（只改第一分支致 top 失配）、D（只改第二分支致 bottom 失配，与 A 对称）。五组覆盖"改第一分支 / 公式替换 / 整体换回 / 改第二分支 / 默认关闭开关"五个角度——全部令 top/bottom 对齐位置错。全部实测（Python 3.9/matplotlib 3.6.0，源码构建 C 扩展，conda 编译器）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
