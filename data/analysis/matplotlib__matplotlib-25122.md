# matplotlib__matplotlib-25122

## 问题背景

`mlab._spectral_helper` 的窗函数归一化（windows correction）用了不必要的 `np.abs`，对含负值的窗（如 flattop）给出错误结果。`np.abs(window).sum() != window.sum()`、`(np.abs(window)**2).sum()` 这些 abs 对负值窗的归一化都不对。Golden patch 去掉所有 `np.abs`：`magnitude`/`complex` 模式用 `window.sum()`，psd 的 scale_by_freq 分支用 `(window**2).sum()`，else 分支用 `window.sum()**2`。

## Golden Patch 语义分析

```python
elif mode == 'magnitude':
    result = np.abs(result) / window.sum()       # was np.abs(window).sum()
...
elif mode == 'complex':
    result /= window.sum()                        # was np.abs(window).sum()
...
if scale_by_freq:
    result /= (window**2).sum()                   # was (np.abs(window)**2).sum()
else:
    result /= window.sum()**2                     # was np.abs(window).sum()**2
```
核心语义：**窗归一化不应对窗取绝对值——直接用 `window.sum()`、`(window**2).sum()`、`window.sum()**2`**。对含负值的窗，`window.sum()`（可能因正负抵消而小）才是正确的归一化因子。关键点：四处都去 abs；其中 F2P 的 flattop（含负值）经 `scale_by_freq=False` 走 else 分支 `window.sum()**2`，与 scale_by_freq=True 的 `(window**2).sum()` 配合断言。

F2P 测试 `test_mlab.py::TestSpectral::test_psd_window_flattop`：用 flattop 窗（含负值），断言 `spec*win.sum()**2 == spec_a*Fs*(win**2).sum()`（density 与 spectrum 归一化一致）。

## 调用链分析

`mlab.psd(window=flattop, scale_by_freq=...)` → `_spectral_helper` → psd 分支按 scale_by_freq 选归一化：True→`(window**2).sum()`、False→`window.sum()**2`。任一处误加 abs、或两分支公式互换，对含负值窗给出错误归一化，F2P 的 density/spectrum 一致性断言失败。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | else 分支 `window.sum()**2`→`np.abs(window).sum()**2` |
| B | 🟢 高质量 | 重做 | scale_by_freq 分支 → `np.abs(window).sum()**2` |
| C | 🟢 高质量 | 重做 | else 分支 → `(window**2).sum()`（公式错） |
| D | 🟢 高质量 | 重做 | scale_by_freq 分支 → `window.sum()**2`（公式错） |
| E | 🟢 高质量 | 重做 | else 归一化藏到模块级 _USE_WINDOW_ABS 开关后 |

原始 A==C（都在 else 分支加 abs）。保留 A，重做 B（scale_by_freq 分支加 abs）、C（else 用平方和）、D（scale_by_freq 用和的平方）、E（模块级开关）。

## 各组 Mutation 分析

### Group A — 保留（C1 值：else 加 abs）
```diff
-            result /= window.sum()**2
+            result /= np.abs(window).sum()**2
```
**变异语义**：scale_by_freq=False 的 else 分支 `window.sum()**2` 改回 `np.abs(window).sum()**2`——还原原 bug：对含负值的 flattop，`abs(window).sum() != window.sum()`，归一化错。F2P（flattop）失败。保留。

### Group B — 重做（C1 值：scale_by_freq 加 abs）
```diff
-            result /= (window**2).sum()
+            result /= np.abs(window).sum()**2
```
**变异语义**：scale_by_freq=True 分支的 `(window**2).sum()`（平方和）改成 `np.abs(window).sum()**2`（abs 和的平方）——两者数学不等（`Σ|w|)² ≠ Σw²`），density 归一化错。作用于另一个分支。F2P 失败。重做为 B。

### Group C — 重做（C1 值：else 用平方和）
```diff
-            result /= window.sum()**2
+            result /= (window**2).sum()
```
**变异语义**：else 分支 `window.sum()**2`（和的平方）改成 `(window**2).sum()`（平方和）——两者数学不等，preserve-power 归一化用错公式（混淆了 density 与 spectrum 的归一化）。F2P 失败。重做为 C（与 A 的"加 abs"不同——这里用了另一分支的公式）。

### Group D — 重做（C1 值：scale_by_freq 用和的平方）
```diff
-            result /= (window**2).sum()
+            result /= window.sum()**2
```
**变异语义**：scale_by_freq=True 分支 `(window**2).sum()`（平方和）改成 `window.sum()**2`（和的平方）——density 归一化用错公式。与 C 对称（两分支互换了归一化公式）。F2P 失败。重做为 D。

### Group E — 重做（E2 隐式→显式开关）
```diff
-            result /= window.sum()**2
+            if globals().get('_USE_WINDOW_ABS', True):
+                result /= np.abs(window).sum()**2
+            else:
+                result /= window.sum()**2
```
**变异语义**：else 分支归一化藏到模块级 `_USE_WINDOW_ABS`（默认 True）开关后——默认走 `np.abs(window).sum()**2`（原 bug），只有设 False 才用正确的 `window.sum()**2`。`_spectral_helper` 是模块函数（无 self），故用 `globals().get(...)` 作开关。默认即 bug。F2P 失败。重做为 E。

## 新设计 Mutation 说明

原始 A==C 字节相同（都在 else 分支加 abs），原 D 仅在 scale_by_freq 分支。本次保留 A（else 加 abs 还原 bug），重做 B（scale_by_freq 分支加 abs）、C（else 用平方和 `(window**2).sum()`）、D（scale_by_freq 用和的平方 `window.sum()**2`，与 C 对称）、E（模块级 `_USE_WINDOW_ABS` 默认开 abs 开关）。五组覆盖"else 加 abs / scale_by_freq 加 abs / else 公式错 / scale_by_freq 公式错 / 默认开关"五个角度，作用于两个归一化分支与特性开关——全部令含负值窗的归一化错误。全部实测（Python 3.9/matplotlib 3.6.0，源码构建 C 扩展，conda 编译器）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
