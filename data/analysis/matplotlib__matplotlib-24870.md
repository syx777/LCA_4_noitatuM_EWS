# matplotlib__matplotlib-24870

## 问题背景

`contour()`/`contourf()` 对 bool 数组应自动检测并默认 levels 为 `[0.5]`（非填充）/`[0, .5, 1]`（填充），而非默认 8 个 levels（0~1.05 挤在一起）。Golden patch 在 `_process_contour_level_args(args, z_dtype)` 里增加 bool 检测分支，并把 z_dtype 一路传进来（改 `_contour_args`/`_check_xyz`/tricontour）。

## Golden Patch 语义分析

```python
def _process_contour_level_args(self, args, z_dtype):
    if self.levels is None:
        if args:
            levels_arg = args[0]
        elif np.issubdtype(z_dtype, bool):
            if self.filled:
                levels_arg = [0, .5, 1]
            else:
                levels_arg = [.5]
        else:
            levels_arg = 7  # Default, hard-wired.
```
核心语义：**当未显式给 levels、无 args、且 `np.issubdtype(z_dtype, bool)` 时，按 filled 与否默认 `[0,.5,1]` 或 `[.5]`**。关键点：`np.issubdtype(..., bool)`（正确识别 numpy bool 数组）、filled→`[0,.5,1]` / 非填充→`[.5]` 的正确映射、`self.levels is None` 守卫。

F2P 测试 `test_contour.py::test_bool_autolevel`：对 bool 数组（list/ndarray/masked），断言 `contour(z).levels==[.5]`、`contourf(z).levels==[0,.5,1]`，及 tricontour 同理。

## 调用链分析

`plt.contour(bool_array)` → `_contour_args` 取 z.dtype → `_process_contour_level_args(args, z.dtype)` → bool 检测 → 设默认 levels。level 值偏移、分支对调、检测 API 错、分支禁用、或门控开关，都会让 bool 数组得不到 `[.5]`/`[0,.5,1]`。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 非填充 level `[.5]`→`[.4]` |
| B | ➕ 新增 | 新增 | filled/非填充分支体对调 |
| C | 🟢 高质量 | 保留 | `np.issubdtype`→`issubclass(...type, bool)` 检测失效 |
| D | 🟢 高质量 | 重做 | bool 分支 `and False` 永不命中 |
| E | 🟢 高质量 | 保留 | bool autodetect 藏到 auto_bool_levels 开关后 |

原始 A/C/D/E，缺 B；D 原为 `if False` 但 `if self.levels is None` 出现两次（OLD 不唯一）。保留 A、C、E，新增 B，重做 D（bool 分支 `and False`）。

## 各组 Mutation 分析

### Group A — 保留（C1 值：level 偏移）
```diff
-                    levels_arg = [.5]
+                    levels_arg = [.4]
```
**变异语义**：非填充 bool 等高线默认 level 由 `[.5]` 改成 `[.4]`——contour(bool) 的 levels 不再是 [.5]。F2P 断言 `levels==[.5]` 失败。值偏移。保留。

### Group B — 新增（D4 状态：分支对调）
```diff
                 if self.filled:
-                    levels_arg = [0, .5, 1]
-                else:
+                else:
+                    levels_arg = [0, .5, 1]
                     levels_arg = [.5]
```
**变异语义**：filled 与非填充两个分支的 level 值对调——filled 得 `[.5]`、非填充得 `[0,.5,1]`，与期望相反。F2P 的 contour/contourf 断言都失败。模拟"if/else 分支体写反"。新增为 B。

### Group C — 保留（A1 接口契约：检测 API 错）
```diff
-            elif np.issubdtype(z_dtype, bool):
+            elif issubclass(z_dtype.type, bool):
```
**变异语义**：bool 检测 `np.issubdtype(z_dtype, bool)` 改成 `issubclass(z_dtype.type, bool)`。numpy 的 `np.bool_` 不是 Python `bool` 的子类，`issubclass` 恒为 False，bool 数组走默认 7 levels（原 bug）。模拟"用错了类型检测 API"。F2P 失败。保留。

### Group D — 重做（B2 禁用分支）
**原**：`if self.levels is None` 改 `if False`（但该行出现两次、OLD 不唯一）。
**最终 mutation**：
```diff
-            elif np.issubdtype(z_dtype, bool):
+            elif np.issubdtype(z_dtype, bool) and False:
```
**变异语义**：bool 分支条件追加 `and False` 使其永不命中——bool 数组落到 else 默认 7 levels，autodetect 失效（原 bug）。短路禁用该分支。与 C（检测 API 错）不同——这里检测对、但被强制短路。F2P 失败。重做为 D（唯一锚点）。

### Group E — 保留（E2 隐式→显式开关）
```diff
-                 **kwargs):
+                 auto_bool_levels=False, **kwargs):
...
+        self.auto_bool_levels = auto_bool_levels
...
-            elif np.issubdtype(z_dtype, bool):
+            elif self.auto_bool_levels and np.issubdtype(z_dtype, bool):
```
**变异语义**：bool autodetect 藏到 `ContourSet(auto_bool_levels=False)` 参数后（默认 False）。默认 bool 数组走默认 7 levels，autodetect 失效。只有显式开启才检测。模拟"把 bool 自动检测做成可配置、默认却关掉"。F2P 失败。保留。

## 新设计 Mutation 说明

原始 A/C/D/E、缺 B，且 D 的 `if self.levels is None` 锚点不唯一。本次保留 A（level 偏移）、C（检测 API 错）、E（auto_bool_levels 默认关闭开关），新增 B（filled/非填充分支对调），重做 D（bool 分支 `and False` 短路、唯一锚点）。五组覆盖"level 偏移 / 分支对调 / 检测 API 错 / 分支短路禁用 / 默认关闭开关"五个角度——全部令 bool 数组得不到正确默认 levels。全部实测（Python 3.9/matplotlib 3.6.0，源码构建 C 扩展，conda 编译器）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
