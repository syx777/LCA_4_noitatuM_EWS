# matplotlib__matplotlib-24149

## 问题背景

`ax.bar` 在 matplotlib 3.6.1 对全 nan 数据抛异常（`StopIteration`），破坏了 seaborn 的直方图功能（它会画一个 nan "幻影"bar 来推进颜色循环）。根因：`Axes._convert_dx` 用 `cbook._safe_first_finite(x0)` 取第一个有限元素，全 nan 时该函数抛 `StopIteration`，而原代码只 `except (TypeError, IndexError, KeyError)` 没捕获 StopIteration。Golden patch 给 x0 和 xconv 两处各加 `except StopIteration:` 块，无有限元素时回退到 `cbook.safe_first_element`（无条件取第一个）。

## Golden Patch 语义分析

```python
try:
    x0 = cbook._safe_first_finite(x0)
except (TypeError, IndexError, KeyError):
    pass
except StopIteration:
    x0 = cbook.safe_first_element(x0)

try:
    x = cbook._safe_first_finite(xconv)
except (TypeError, IndexError, KeyError):
    x = xconv
except StopIteration:
    x = cbook.safe_first_element(xconv)
```
核心语义：**`_safe_first_finite` 在全 nan（无有限元素）时抛 `StopIteration`，必须为 x0 和 xconv 两处各加 `except StopIteration` 块，回退到 `safe_first_element`（无条件取首元素）**。关键点：捕获的异常类型是 `StopIteration`、两处都要加、兜底用的是 `safe_first_element`（不再抛 StopIteration）。

F2P 测试 `test_axes.py::test_bar_all_nan`（check_figures_equal）：`ax.bar([np.nan],[np.nan])` 后再 `ax.bar([1],[1])`，断言与参考图一致（即全 nan bar 不崩溃）。

## 调用链分析

`ax.bar` → `_convert_dx(width, x0, x, convert)` → `cbook._safe_first_finite(x0)`（全 nan → StopIteration）→ golden 的 `except StopIteration: x0 = safe_first_element(x0)` 兜底。捕获错类型、删兜底块、兜底用错函数、或门控开关，都会让全 nan 时 StopIteration 上抛、ax.bar 崩溃。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | x0 块 `except StopIteration`→`except ValueError`，类型错 |
| B | 🟢 高质量 | 保留 | 删除 x0 与 xconv 两个 StopIteration 块 |
| C | ➕ 补充 | 重做 | x0 兜底 `safe_first_element`→`_safe_first_finite`，兜底再抛 |
| D | ➕ 补充 | 重做 | 只删 xconv 的 StopIteration 块 |
| E | 🟢 高质量 | 保留 | 两块藏到 `_nan_fallback` 开关后（否则 raise） |

原始 B==C（都删两块）、D==E（都只删 x0 块），实际只有"删块"一种机制。保留 A（类型错）、B（删两块）、E（开关），重做 C（兜底用错函数）、D（只删 xconv 块）。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：捕获错类型）
```diff
-            except StopIteration:
+            except ValueError:
                 x0 = cbook.safe_first_element(x0)
```
**变异语义**：x0 的 `except StopIteration` 改成 `except ValueError`。全 nan 时 `_safe_first_finite` 抛的是 StopIteration（不匹配 ValueError）→ 异常上抛、不走 safe_first_element 兜底，ax.bar 崩溃。模拟"捕获了错误的异常类型"。F2P 失败。保留。

### Group B — 保留（B2 删除兜底块）
```diff
-            except StopIteration:
-                # ... fall back to first element unconditionally
-                x0 = cbook.safe_first_element(x0)
             try:
                 x = cbook._safe_first_finite(xconv)
-            except StopIteration:
-                x = cbook.safe_first_element(xconv)
```
**变异语义**：删除 x0 和 xconv 两个 `except StopIteration` 兜底块。全 nan 时 StopIteration 无人捕获、上抛，ax.bar 崩溃。还原原 bug。F2P 失败。保留。

### Group C — 重做（A1 接口契约：兜底用错函数）
**原**：与 B 相同（删两块）。
**最终 mutation**：
```diff
             except StopIteration:
-                x0 = cbook.safe_first_element(x0)
+                x0 = cbook._safe_first_finite(x0)
```
**变异语义**：x0 兜底里 `cbook.safe_first_element(x0)` 改回 `cbook._safe_first_finite(x0)`——兜底又调了会抛 StopIteration 的那个函数。全 nan 时 except 块捕获了 StopIteration，但兜底自身再次抛 StopIteration（无人接），ax.bar 仍崩溃。模拟"兜底逻辑用错了函数（finite vs element）"。比 B 隐蔽——except 块还在、看着像处理了。F2P 失败。重做为 C。

### Group D — 重做（B2 部分删除）
**原**：与 E 相同（只删 x0 块）。
**最终 mutation**：
```diff
             try:
                 x = cbook._safe_first_finite(xconv)
             except (TypeError, IndexError, KeyError):
                 x = xconv
-            except StopIteration:
-                x = cbook.safe_first_element(xconv)
```
**变异语义**：只删除 xconv 的 `except StopIteration` 块（保留 x0 的）。x0 兜底成功，但 xconv 处全 nan 时 StopIteration 上抛，ax.bar 在 x 转换处崩溃。部分还原 bug——比 B（删两块）更局部。模拟"补丁只打了一半"。F2P 失败。重做为 D。

### Group E — 保留（E2 隐式→显式开关）
```diff
             except StopIteration:
-                x0 = cbook.safe_first_element(x0)
+                if getattr(self, '_nan_fallback', False):
+                    x0 = cbook.safe_first_element(x0)
+                else:
+                    raise
```
**变异语义**：两个 StopIteration 兜底藏到 `_nan_fallback` 开关后（默认 False 则 `raise` 重抛）。默认全 nan 时 re-raise StopIteration，ax.bar 崩溃。只有显式开启才兜底。模拟"把 nan 兜底做成可配置、默认却关掉"。F2P 失败。保留。

## 新设计 Mutation 说明

原始 B==C（删两块）、D==E（只删 x0 块），实际只有"删块"一种机制。本次保留 A（except 类型错 ValueError）、B（删两块还原 bug）、E（_nan_fallback 默认关闭开关），重做 C（兜底误用 `_safe_first_finite` 致再次抛 StopIteration）、D（只删 xconv 块、部分还原）。五组覆盖"捕获错类型 / 删两块 / 兜底用错函数 / 部分删块 / 默认关闭开关"五个角度——全部令全 nan 数据时 StopIteration 上抛、ax.bar 崩溃。全部实测（Python 3.9/matplotlib 3.6.0，源码构建 C 扩展，conda 编译器）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
