# matplotlib__matplotlib-25287

## 问题背景

offsetText（坐标轴指数标签，如 `1e9`）的颜色基于 `tick.color` 而非 `tick.labelcolor`。设了 `ytick.labelcolor='red'` 后，指数标签颜色没变（仍用 `ytick.color`）。Golden patch 在 `XAxis._init`/`YAxis._init` 里计算 offsetText 颜色时：若 `labelcolor == 'inherit'` 则用 `tick.color`，否则用 `tick.labelcolor`。

## Golden Patch 语义分析

```python
if mpl.rcParams['xtick.labelcolor'] == 'inherit':
    tick_color = mpl.rcParams['xtick.color']
else:
    tick_color = mpl.rcParams['xtick.labelcolor']
self.offsetText.set(..., color=tick_color)
```
核心语义：**offsetText 颜色应遵循 labelcolor 语义——`labelcolor == 'inherit'`（默认）时回退到 `tick.color`，否则用 `labelcolor`**。x 轴和 y 轴各一份。关键点：判断 `== 'inherit'`、inherit 分支取 `color`、else 分支取 `labelcolor`。

F2P 测试 `test_axes.py::test_xaxis_offsetText_color` + `test_yaxis_offsetText_color`：设 labelcolor='blue' 断言 offsetText 为 blue；设 labelcolor='inherit'+color='yellow' 断言为 yellow。

## 调用链分析

`plt.axes()` → `XAxis._init`/`YAxis._init` → 计算 tick_color（inherit?color:labelcolor）→ `offsetText.set(color=tick_color)`。条件反转、分支对调、恒取 color、恒取 labelcolor、或门控开关，都会让 offsetText 颜色不遵循 labelcolor。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `== 'inherit'`→`!= 'inherit'`，条件反转 |
| B | ➕ 补充 | 重做 | if/else 分支体对调 |
| C | 🟢 高质量 | 保留 | 恒取 `tick.color`（还原 bug） |
| D | 🟢 高质量 | 保留 | 恒取 `tick.labelcolor`，inherit 当颜色 |
| E | 🟢 高质量 | 重做 | labelcolor 逻辑藏到 _use_labelcolor 开关后 |

原始 A==B==E（都条件反转 `!=`）。保留 A、C、D，重做 B（分支对调）、E（开关）。

## 各组 Mutation 分析

### Group A — 保留（B3 条件反转）
```diff
-        if mpl.rcParams['xtick.labelcolor'] == 'inherit':
+        if mpl.rcParams['xtick.labelcolor'] != 'inherit':
```
**变异语义**：xtick/ytick 的 `labelcolor == 'inherit'` 都反转成 `!= 'inherit'`。inherit 时反而取 labelcolor（='inherit' 字符串当颜色，非法或非预期）、非 inherit 时取 color（忽略用户 labelcolor）。逻辑颠倒。F2P 两个断言都失败。保留。

### Group B — 重做（D4 状态：分支对调）
**原**：与 A 相同（条件反转）。
**最终 mutation**：
```diff
         if mpl.rcParams['xtick.labelcolor'] == 'inherit':
-            tick_color = mpl.rcParams['xtick.color']
-        else:
+        else:
+            tick_color = mpl.rcParams['xtick.color']
            （inherit 分支改取 labelcolor、else 改取 color）
```
**变异语义**：保持 `== 'inherit'` 条件不变，但把 if/else 两个分支体对调——inherit 时取 `labelcolor`（='inherit'）、非 inherit 时取 `color`。与 A（反转条件）效果相同但机制不同：条件对、分支体错位。offsetText 在设了 labelcolor 时反而用 color。F2P 失败。重做为 B。

### Group C — 保留（C1 值：恒取 color）
```diff
-        if mpl.rcParams['xtick.labelcolor'] == 'inherit':
-            tick_color = mpl.rcParams['xtick.color']
-        else:
-            tick_color = mpl.rcParams['xtick.labelcolor']
+        tick_color = mpl.rcParams['xtick.color']
```
**变异语义**：删除 inherit 判断，offsetText 颜色恒取 `xtick.color`/`ytick.color`——还原原 bug：labelcolor 设置被完全忽略，offsetText 总用 tick color。F2P 断言 labelcolor='blue' 实得 color → 失败。保留。

### Group D — 保留（C1 值：恒取 labelcolor）
```diff
-        if mpl.rcParams['xtick.labelcolor'] == 'inherit':
-            tick_color = mpl.rcParams['xtick.color']
-        else:
-            tick_color = mpl.rcParams['xtick.labelcolor']
+        tick_color = mpl.rcParams['xtick.labelcolor']
```
**变异语义**：颜色恒取 `xtick.labelcolor`/`ytick.labelcolor`——当 labelcolor='inherit'（默认）时，offsetText 颜色被设成字面字符串 'inherit' 而非继承 tick color。F2P 的 inherit 子断言（期望 color 值）失败。模拟"漏了 inherit 回退、直接用 labelcolor"。保留。

### Group E — 重做（E2 隐式→显式开关）
**原**：与 A 相同（条件反转）。
**最终 mutation**：
```diff
-        if mpl.rcParams['xtick.labelcolor'] == 'inherit':
-            tick_color = mpl.rcParams['xtick.color']
-        else:
+        if getattr(self, '_use_labelcolor', False) and mpl.rcParams['xtick.labelcolor'] != 'inherit':
+        else:
+            tick_color = mpl.rcParams['xtick.color']
            （labelcolor 分支门控）
```
**变异语义**：labelcolor 逻辑藏到 `_use_labelcolor` 开关后（默认 False）——默认恒走 else 取 `tick.color`，labelcolor 设置被忽略，还原原 bug。只有显式开启才用 labelcolor。模拟"把 labelcolor 支持做成可配置、默认却关掉"。F2P 失败。重做为 E。

## 新设计 Mutation 说明

原始 A==B==E 字节相同（都条件反转 `== 'inherit'`→`!= 'inherit'`）。本次保留 A（条件反转）、C（恒取 color 还原 bug）、D（恒取 labelcolor、inherit 当颜色），重做 B（if/else 分支体对调，与 A 的反转条件区分）、E（_use_labelcolor 默认关闭开关）。五组覆盖"条件反转 / 分支对调 / 恒取 color / 恒取 labelcolor / 默认关闭开关"五个角度，x 轴/y 轴对称应用——全部令 offsetText 颜色不遵循 labelcolor。全部实测（Python 3.9/matplotlib 3.7.0，源码构建 C 扩展，conda 编译器）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
