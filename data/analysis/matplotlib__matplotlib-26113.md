# matplotlib__matplotlib-26113

## 问题背景

`hexbin` 的 `mincnt` 参数行为不一致：有无 `C` 参数时 mincnt 的边界判定不同。无 C 时 mincnt=1 显示计数 >=1 的格子；有 C 时（走 accum 分支）用 `len(acc) > mincnt`（严格大于），导致 mincnt=1 时只显示计数 >1 的格子。Golden patch 把有 C 分支的 `len(acc) > mincnt` 改成 `>= mincnt`，与无 C 路径一致。

## Golden Patch 语义分析

```python
if mincnt is None:
    mincnt = 0
accum = np.array(
    [reduce_C_function(acc) if len(acc) >= mincnt else np.nan
     for Cs_at_i in [Cs_at_i1, Cs_at_i2]
     for acc in Cs_at_i[1:]],
    float)
```
核心语义：**有 C 参数时，格子计数 `len(acc)` 与 mincnt 的比较应为 `>= mincnt`（与无 C 路径一致），而非 `> mincnt`**。关键点：比较运算符 `>=`、阈值 `mincnt`（不偏移）。

F2P 测试 `test_axes.py::test_hexbin_mincnt_behavior_upon_C_parameter`（check_figures_equal）：相同数据，无 C 的 hexbin(mincnt=1) 与有 C 的 hexbin(C=..., mincnt=1) 应渲染一致。

## 调用链分析

`ax.hexbin(C=..., mincnt=1)` → accum 分支 `len(acc) >= mincnt` 决定每个格子是 reduce 还是 nan。比较运算符、阈值出错，则有 C 路径的格子可见性与无 C 路径不一致，F2P 两图不等。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `>= mincnt`→`> mincnt`（还原 bug） |
| B | ➕ 补充 | 重做 | `>= mincnt`→`< mincnt`，比较反转 |
| C | ➕ 补充 | 重做 | `>= mincnt`→`>= mincnt + 1`，阈值偏移 |
| D | ➕ 补充 | 重做 | `>= mincnt`→`== mincnt`，只取等于 |
| E | 🟢 高质量 | 重做 | 比较藏到 _HEXBIN_GE_MINCNT 开关后 |

原始 B==C==E 全部与 A 相同（`>=`→`>`），单行 patch 实际只有 1 个 mutation。保留 A（还原 bug），重做 B（`<`）、C（`>=+1`）、D（`==`）、E（开关）。

## 各组 Mutation 分析

### Group A — 保留（B3 还原 bug：> mincnt）
```diff
-                [reduce_C_function(acc) if len(acc) >= mincnt else np.nan
+                [reduce_C_function(acc) if len(acc) > mincnt else np.nan
```
**变异语义**：`>= mincnt` 改回 `> mincnt`——还原原 bug：有 C 时 mincnt 边界比无 C 时严一格，mincnt=1 时计数恰为 1 的格子被排除（nan），与无 C 路径不一致。F2P 失败。保留。

### Group B — 重做（B3 比较反转：< mincnt）
**原**：与 A 相同（`>`）。
**最终 mutation**：
```diff
-                [reduce_C_function(acc) if len(acc) >= mincnt else np.nan
+                [reduce_C_function(acc) if len(acc) < mincnt else np.nan
```
**变异语义**：`>= mincnt` 改成 `< mincnt`——计数 < mincnt 的格子才 reduce、>= 反而 nan，可见性完全颠倒，几乎所有有数据的格子取 nan。F2P 失败。重做为 B。

### Group C — 重做（C1 值：阈值偏移）
**原**：与 A 相同（`>`）。
**最终 mutation**：
```diff
-                [reduce_C_function(acc) if len(acc) >= mincnt else np.nan
+                [reduce_C_function(acc) if len(acc) >= mincnt + 1 else np.nan
```
**变异语义**：阈值 `>= mincnt` 改成 `>= mincnt + 1`——等价于 `> mincnt`，mincnt=1 时计数=1 的格子被排除。与 A 效果相近但形式是阈值 +1（而非改运算符）。F2P 失败。重做为 C。

### Group D — 重做（B3 比较运算符：== mincnt）
**原**：与 A 相同（`>`）。
**最终 mutation**：
```diff
-                [reduce_C_function(acc) if len(acc) >= mincnt else np.nan
+                [reduce_C_function(acc) if len(acc) == mincnt else np.nan
```
**变异语义**：`>= mincnt` 改成 `== mincnt`——只有计数恰等于 mincnt 的格子 reduce、其它（含 > mincnt 的）全 nan，绝大多数格子丢失。比较运算符错。F2P 失败。重做为 D。

### Group E — 重做（E2 隐式→显式开关）
**原**：与 A 相同（`>`）。
**最终 mutation**：
```diff
+            _ge = globals().get('_HEXBIN_GE_MINCNT', False)
             accum = np.array(
-                [reduce_C_function(acc) if len(acc) >= mincnt else np.nan
+                [reduce_C_function(acc) if (len(acc) >= mincnt if _ge else len(acc) > mincnt) else np.nan
```
**变异语义**：mincnt 比较藏到模块级 `_HEXBIN_GE_MINCNT`（默认 False）开关后——默认走 `> mincnt`（原 bug），只有设 True 才用 `>= mincnt`。`hexbin` 是方法但此处用 `globals()` 模块级开关。默认即 bug。F2P 失败。重做为 E。

## 新设计 Mutation 说明

原始 B==C==E 全部与 A 字节相同（`>=`→`>`），单行 patch 实际只有"还原 bug"一种 mutation。本次保留 A（`> mincnt` 还原 bug），重做 B（`< mincnt` 反转）、C（`>= mincnt+1` 阈值偏移）、D（`== mincnt` 只取等于）、E（`_HEXBIN_GE_MINCNT` 默认关闭开关）。五组覆盖"还原 bug / 比较反转 / 阈值偏移 / 只取等于 / 默认关闭开关"五个角度，围绕同一行比较表达式分化——全部令有 C 路径的格子可见性与无 C 路径不一致。全部实测（Python 3.9/matplotlib 3.7.1，源码构建 C 扩展，conda 编译器）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
