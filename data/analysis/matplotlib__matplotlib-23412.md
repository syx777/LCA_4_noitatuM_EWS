# matplotlib__matplotlib-23412

## 问题背景

patch 对象的 dash linestyle offset 无效。用 dash tuple（如 `ls=(10,(10,10))`）设 patch 边线样式时，offset（第一个分量）不起作用——patch 边线虚线总是从 offset 0 开始。根因：`Patch.draw` 里用 `cbook._setattr_cm` 临时把 `_dash_pattern` 的 offset 强制置 0（`(0, self._dash_pattern[1])`），传统上 patch 忽略 dashoffset。Golden patch 改成传入完整的 `self._dash_pattern`（保留 offset）。

## Golden Patch 语义分析

```python
def draw(self, renderer):
    if not self.get_visible():
        return
    with cbook._setattr_cm(
             self, _dash_pattern=(self._dash_pattern)), \
         self._bind_draw_path_function(renderer) as draw_path:
        ...
```
核心语义：**绘制 patch 时应传入完整的 `self._dash_pattern`（含 offset），而非把 offset 强制置 0**。原代码 `(0, self._dash_pattern[1])` 丢掉了 offset。关键点：传完整 `(offset, onoff)` 元组，不在 draw、`_set_linewidth`、`set_linestyle` 任何环节把 offset 清零。

F2P 测试 `test_patches.py::test_dash_offset_patch_draw`（check_figures_equal）：用 `linestyle=(6,[6,6])` 创建 Rectangle，断言 `get_linestyle() == (6, [6, 6])`（offset 保留），并对比渲染。

## 调用链分析

`Rectangle(linestyle=(6,[6,6]))` → `set_linestyle` → `_unscaled_dash_pattern = _get_dash_pattern(ls)` → `_set_linewidth` → `_dash_pattern = _scale_dashes(*unscaled, w)` → `draw` 用 `_setattr_cm(_dash_pattern=...)`。offset 在 set_linestyle 解析、_scale_dashes 缩放、draw 绘制任一环节被清零，都会让 offset 丢失。F2P 检查 `get_linestyle()`（读 `_unscaled_dash_pattern`）和渲染。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | draw 用 `(0, ...[1])` 强制 offset=0，还原 bug |
| B | 🟢 高质量 | 保留 | draw 用 `(...[1])` 只取 onoff，丢 offset 维 |
| C | 🟢 高质量 | 保留 | _set_linewidth 的 `_scale_dashes(0, dashes, w)` 写死 offset 0 |
| D | 🟢 高质量 | 保留 | set_linestyle 把 unscaled offset 强制改 0 |
| E | 🟢 高质量 | 保留 | draw offset 藏到 `_use_dash_offset` 开关后 |

五组作用于不同环节（draw×2、_set_linewidth、set_linestyle、开关），全部保留并核验。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：draw 强制 offset 0）
```diff
-        with cbook._setattr_cm(self, _dash_pattern=(self._dash_pattern)), \
+        with cbook._setattr_cm(self, _dash_pattern=(0, self._dash_pattern[1])), \
```
**变异语义**：还原原始 bug——draw 时把 `_dash_pattern` 的 offset 强制置 0（`(0, self._dash_pattern[1])`）。绘制阶段丢掉 offset，patch 边线虚线偏移失效。F2P 渲染对比失败。保留。

### Group B — 保留（C1 类型：只取 onoff）
```diff
-        with cbook._setattr_cm(self, _dash_pattern=(self._dash_pattern)), \
+        with cbook._setattr_cm(self, _dash_pattern=(self._dash_pattern[1])), \
```
**变异语义**：`_dash_pattern=(self._dash_pattern[1])` 只取 dash pattern 的第二维（onoff 序列），丢掉 offset 维。传给 `_setattr_cm` 的不再是 `(offset, onoff)` 元组而是裸 onoff 列表——dash 结构错、offset 丢失，绘制异常。比 A 更彻底地破坏 dash_pattern 结构。F2P 失败。保留。

### Group C — 保留（C1 值：_scale_dashes offset 0）
```diff
-        self._dash_pattern = mlines._scale_dashes(
-            *self._unscaled_dash_pattern, w)
+        offset, dashes = self._unscaled_dash_pattern
+        self._dash_pattern = mlines._scale_dashes(0, dashes, w)
```
**变异语义**：`_set_linewidth` 里缩放 dash 时把 offset 写死为 0（`_scale_dashes(0, dashes, w)`）——offset 在 dash pattern 计算阶段就丢失。比 draw 阶段（A/B）更上游。get_linestyle 间接受影响、渲染 offset 为 0。F2P 失败。保留。

### Group D — 保留（D1 状态：set_linestyle 清 offset）
```diff
-        self._unscaled_dash_pattern = mlines._get_dash_pattern(ls)
+        unscaled = mlines._get_dash_pattern(ls)
+        if isinstance(unscaled, tuple) and len(unscaled) == 2:
+            self._unscaled_dash_pattern = (0, unscaled[1])
+        else:
+            self._unscaled_dash_pattern = unscaled
```
**变异语义**：`set_linestyle` 里把解析出的 unscaled dash pattern 的 offset 强制改成 0（`(0, unscaled[1])`）——linestyle 存储阶段（最上游）就丢掉用户 offset。`get_linestyle()` 直接返回 `(0, [6,6])` 而非 `(6, [6,6])`，F2P 断言失败。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
-        with cbook._setattr_cm(self, _dash_pattern=(self._dash_pattern)), \
+        with cbook._setattr_cm(self, _dash_pattern=(self._dash_pattern if getattr(self, '_use_dash_offset', False) else (0, self._dash_pattern[1]))), \
```
**变异语义**：draw 的 dash offset 藏到 `_use_dash_offset` 开关后（默认 False 走 `(0, ...[1])`）。默认绘制时 offset 置 0，patch 虚线偏移失效。只有显式开启才用真实 offset。模拟"把 dash offset 支持做成可配置、默认却关掉"。F2P 失败。保留。

## 新设计 Mutation 说明

本实例原五组机制已各不相同且全部有效，无重复，故全部保留并逐一核验。五组覆盖"draw 强制 offset 0 / draw 只取 onoff 丢维 / _scale_dashes 写死 0 / set_linestyle 清 offset / 默认关闭开关"五个角度，分别作用于 draw 绘制、dash 结构、linewidth 缩放、linestyle 解析、特性开关五个环节——全部令 patch dash offset 丢失。全部实测（Python 3.9/matplotlib 3.5.2，源码构建 C 扩展）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
