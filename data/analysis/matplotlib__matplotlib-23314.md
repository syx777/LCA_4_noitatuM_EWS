# matplotlib__matplotlib-23314

## 问题背景

`set_visible(False)` 对 3D 投影子图无效——即使设为不可见，subplot 仍然显示。根因：`Axes3D.draw()` 没有像 2D Axes 那样在开头检查自身可见性。Golden patch 在 `Axes3D.draw` 开头加 `if not self.get_visible(): return`，使不可见的 3D 轴跳过绘制。

## Golden Patch 语义分析

```python
@martist.allow_rasterization
def draw(self, renderer):
    if not self.get_visible():
        return
    self._unstale_viewLim()
    ...
```
核心语义：**`Axes3D.draw` 开头必须检查 `self.get_visible()`，不可见则直接 return 跳过绘制**。关键点：检查的是 `self`（Axes3D 本身）的可见性、用 `not ... : return` 短路、且无条件执行（不门控）。

F2P 测试 `test_mplot3d.py::test_invisible_axes`（check_figures_equal）：创建 3D 子图、`set_visible(False)`，断言渲染结果与空参考图一致（即不可见轴不绘制）。

## 调用链分析

figure 渲染 → `Axes3D.draw(renderer)` → `if not self.get_visible(): return`（golden）→ 否则继续绘制背景、坐标轴等。检查对象错（patch/figure）、条件反转、删守卫、或门控开关，都会让不可见的 3D 轴仍被绘制。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `self.get_visible()`→`self.patch.get_visible()`，查错对象 |
| B | 🟢 高质量 | 保留 | 去掉 `not`，可见性逻辑反转 |
| C | 🟢 高质量 | 重做 | `self.get_visible()`→`self.figure.get_visible()`，查错层级 |
| D | 🟢 高质量 | 保留 | 删除整个可见性守卫 |
| E | 🟢 高质量 | 保留 | 守卫藏到 draw(check_visibility=False) 参数后 |

原始 B 与 C 字节相同（都 `if self.get_visible()`）。保留 A/B/D/E，重做 C 为 `self.figure.get_visible()`。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：查 patch）
```diff
-        if not self.get_visible():
+        if not self.patch.get_visible():
```
**变异语义**：可见性检查对象从 `self`（Axes3D）改成 `self.patch`（背景 patch）。`set_visible(False)` 作用于 Axes3D 时其 patch 仍可见 → draw 不短路 → 3D 子图仍绘制。模拟"取错了可见性对象（轴 vs 其背景 patch）"。F2P 失败。保留。

### Group B — 保留（B3 条件反转：去 not）
```diff
-        if not self.get_visible():
+        if self.get_visible():
             return
```
**变异语义**：去掉 `not`——可见时反而 return 不画、不可见时继续画。可见性逻辑完全颠倒。set_visible(True) 的正常轴反而不绘制、set_visible(False) 的仍绘制。F2P 失败。保留。

### Group C — 重做（A1 接口契约：查 figure）
**原**：与 B 字节相同（`if self.get_visible()`）。
**最终 mutation**：
```diff
-        if not self.get_visible():
+        if not self.figure.get_visible():
```
**变异语义**：检查对象从 `self.get_visible()` 改成 `self.figure.get_visible()`——查的是整个 figure 的可见性而非该 Axes3D。`set_visible(False)` 作用于单个 axes 时 figure 仍可见 → draw 不短路 → 3D 子图仍绘制。模拟"取错了可见性层级（axes vs figure）"。与 A（查 patch）都是错对象但层级不同。F2P 失败。重做为 C。

### Group D — 保留（B2 删守卫）
```diff
-        if not self.get_visible():
-            return
         self._unstale_viewLim()
```
**变异语义**：删除整个 `if not self.get_visible(): return` 守卫——draw 不再检查可见性，set_visible(False) 完全无效，还原原 bug。F2P 失败。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
-    def draw(self, renderer):
-        if not self.get_visible():
+    def draw(self, renderer, check_visibility=False):
+        if check_visibility and not self.get_visible():
             return
```
**变异语义**：可见性短路藏到 `draw(check_visibility=False)` 参数后（默认 False）。渲染流程调用 `draw(renderer)` 不传该参数 → `False and ...` 短路 → 不检查可见性，set_visible(False) 无效。模拟"把可见性检查做成可配置、默认却关掉"。F2P 失败。保留。

## 新设计 Mutation 说明

原始 B、C 字节完全相同（`if self.get_visible()`）。本次保留 A（查 patch）、B（去 not 反转）、D（删守卫）、E（check_visibility 默认关闭开关），重做 C 为 `self.figure.get_visible()`（查错层级，与 A 的查 patch 区分）。五组覆盖"查 patch / 条件反转 / 查 figure / 删守卫 / 默认关闭开关"五个角度——全部令不可见 3D 轴仍被绘制。全部实测（Python 3.9/matplotlib 3.5.2，源码构建 C 扩展）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
