# matplotlib__matplotlib-24627

## 问题背景

`cla()`/`clf()` 应解除被移除 artist 的 `.axes` 和 `.figure` 属性。从 axes 单独 remove 一个 artist 会解除其 `.axes`，但 `ax.cla()`（清空整个 axes）却不解除子 artist 的 `.axes`/`.figure`——它们仍指向已被清空的 axes/figure。Golden patch 在 `_AxesBase.__clear` 里，把旧 children 列表保存下来后遍历，逐个把 `chld.axes = chld.figure = None`。

## Golden Patch 语义分析

```python
old_children, self._children = self._children, []
for chld in old_children:
    chld.axes = chld.figure = None
```
核心语义：**清空 children 前先保存旧列表 `old_children`，再遍历它把每个子 artist 的 `axes` 和 `figure` **都**解除为 None**。关键点：遍历的是 `old_children`（保存的旧列表，非已置空的 `self._children`）、同时解除 `axes` 和 `figure` 两个属性、无条件执行。

F2P 测试 `test_axes.py::test_cla_clears_children_axes_and_fig`：plot 若干 line + imshow，`ax.clear()` 后断言每个 artist 的 `.axes is None` 且 `.figure is None`。

## 调用链分析

`ax.clear()` → `__clear()` → `old_children, self._children = self._children, []` → `for chld in old_children: chld.axes = chld.figure = None`。若只解除一个属性、遍历空列表、删整个循环、或门控开关，则子 artist 的 axes/figure 残留，F2P 断言 None 失败。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 只 `chld.axes = None`，figure 残留 |
| B | 🟢 高质量 | 保留 | 删除整个解除循环 |
| C | ➕ 补充 | 重做 | 只 `chld.figure = None`，axes 残留 |
| D | ➕ 补充 | 重做 | 遍历 `self._children`（已空），循环不执行 |
| E | 🟢 高质量 | 保留 | 解除藏到 `clear_children_axes` 开关后 |

原始 A==C==D（都只 `chld.axes = None`），实际只有"漏解 figure"和"删循环"两种机制。保留 A（漏解 figure）、B（删循环）、E（开关），重做 C（只解 figure 漏 axes）、D（遍历空列表）。

## 各组 Mutation 分析

### Group A — 保留（D1 状态：漏解 figure）
```diff
         for chld in old_children:
-            chld.axes = chld.figure = None
+            chld.axes = None
```
**变异语义**：只 `chld.axes = None`，未解除 `chld.figure`。cla() 后子 artist 的 `axes` 为 None 但 `figure` 仍指向原 figure。F2P 断言 `figure is None` 失败。模拟"链式赋值拆开后漏了一个"。保留。

### Group B — 保留（B2 删循环）
```diff
         old_children, self._children = self._children, []
-        for chld in old_children:
-            chld.axes = chld.figure = None
```
**变异语义**：删除整个 `for chld in old_children: ...` 循环。cla() 完全不解除子 artist 的 axes/figure，两者都残留。还原原 bug。F2P 失败。保留。

### Group C — 重做（D1 状态：漏解 axes）
**原**：与 A 相同（只 `chld.axes = None`）。
**最终 mutation**：
```diff
         for chld in old_children:
-            chld.axes = chld.figure = None
+            chld.figure = None
```
**变异语义**：只 `chld.figure = None`，未解除 `chld.axes`。cla() 后子 artist 的 `figure` 为 None 但 `axes` 仍指向原 axes。F2P 断言 `axes is None` 失败。与 A 对称（A 漏 figure、C 漏 axes）。重做为 C。

### Group D — 重做（D1 状态：遍历空列表）
**原**：与 A 相同。
**最终 mutation**：
```diff
-        for chld in old_children:
+        for chld in self._children:
             chld.axes = chld.figure = None
```
**变异语义**：遍历目标从 `old_children`（保存的旧列表）改成 `self._children`——而上一行刚把 `self._children` 重置为空列表 `[]`，循环体一次都不执行，所有子 artist 的 axes/figure 都不被解除。模拟"遍历了错误的（已被置空的）集合"。两个属性都残留。F2P 失败。重做为 D。

### Group E — 保留（E2 隐式→显式开关）
```diff
-    def __clear(self):
+    def __clear(self, clear_children_axes=False):
...
         for chld in old_children:
-            chld.axes = chld.figure = None
+            if clear_children_axes:
+                chld.axes = chld.figure = None
```
**变异语义**：解除 axes/figure 藏到 `__clear(clear_children_axes=False)` 参数后（默认 False）。`clear()` 调用 `__clear()` 不传该参数 → 不解除，子 artist 属性残留。只有显式开启才解除。模拟"把属性解除做成可配置、默认却关掉"。F2P 失败。保留。

## 新设计 Mutation 说明

原始 A==C==D 字节相同（都只 `chld.axes = None`），实际只有"漏解 figure"和"删循环"两种机制。本次保留 A（漏解 figure）、B（删循环还原 bug）、E（clear_children_axes 默认关闭开关），重做 C（只解 figure、漏 axes，与 A 对称）、D（遍历已置空的 self._children、循环不执行）。五组覆盖"漏解 figure / 删循环 / 漏解 axes / 遍历空列表 / 默认关闭开关"五个角度——全部令 cla() 后子 artist 的 axes 或 figure 残留。全部实测（Python 3.9/matplotlib 3.6.0，源码构建 C 扩展，conda 编译器）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
