# matplotlib__matplotlib-26466

## 问题背景

把数组作为 `annotate` 的 `xy` 参数传入后，修改该数组会改变已创建的 annotation（箭头位置随之变）。根因：`_AnnotationBase.__init__` 直接 `self.xy = xy` 存了数组引用；`OffsetFrom.__init__` 同样 `self._ref_coord = ref_coord` 存引用。Golden patch 在两处用解包 `x, y = xy; self.xy = x, y`（解包成元组即复制，且顺带检查 shape）来切断引用。

## Golden Patch 语义分析

```python
# OffsetFrom.__init__
x, y = ref_coord  # Make copy when ref_coord is an array (and check the shape).
self._ref_coord = x, y

# _AnnotationBase.__init__
x, y = xy  # Make copy when xy is an array (and check the shape).
self.xy = x, y
```
核心语义：**xy/ref_coord 必须解包成 `(x, y)` 元组存储（复制值、切断对原数组的引用），而非直接存原对象**。关键点：两处 `__init__` 都用 `x, y = ...; self.attr = x, y` 模式。

F2P 测试 `test_text.py::test_annotate_and_offsetfrom_copy_input`（check_figures_equal）：用 OffsetFrom 和 annotate 两种方式传数组，事后修改数组，断言 annotation 位置不受影响（test 图 vs ref 图一致）。

## 调用链分析

`ax.annotate(xy=arr)` → `_AnnotationBase.__init__` → `self.xy = x, y`（复制）。`OffsetFrom(l, of_xy)` → `OffsetFrom.__init__` → `self._ref_coord = x, y`（复制）。事后改 arr。任一处存原引用、或对 ndarray 特判跳过复制、或门控开关，外部修改会改 annotation，F2P 图不一致。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 新增 | 新增 | OffsetFrom 不解包，存 ref_coord 原引用 |
| B | 🟢 高质量 | 保留 | _AnnotationBase 对 ndarray 特判跳过复制 |
| C | 🟢 高质量 | 保留 | _AnnotationBase 直接 `self.xy = xy` |
| D | ➕ 新增 | 新增 | OffsetFrom 解包但存 ref_coord 原引用 |
| E | 🟢 高质量 | 保留 | _AnnotationBase 复制藏到 _copy_input 开关后 |

原始 C==D（都 `self.xy = xy`），缺 A。保留 B、C、E（_AnnotationBase 路径），新增 A、D（OffsetFrom 路径，覆盖 F2P 的 OffsetFrom 子用例）。

## 各组 Mutation 分析

### Group A — 新增（D1 状态：OffsetFrom 不解包）
```diff
         self._artist = artist
-        x, y = ref_coord  # Make copy when ref_coord is an array (and check the shape).
-        self._ref_coord = x, y
+        self._ref_coord = ref_coord
```
**变异语义**：OffsetFrom.__init__ 的解包复制 `x, y = ref_coord; self._ref_coord = x, y` 改成直接 `self._ref_coord = ref_coord`——存原数组引用，事后修改 of_xy 会改 annotation 位置。OffsetFrom 路径不复制。`test_annotate_and_offsetfrom_copy_input`（OffsetFrom 子用例）失败。新增为 A。

### Group B — 保留（B3 条件：ndarray 特判跳过）
```diff
-        x, y = xy  # Make copy when xy is an array (and check the shape).
-        self.xy = x, y
+        import numpy as np
+        if isinstance(xy, np.ndarray):
+            self.xy = xy
+        else:
+            x, y = xy  # Make copy ...
+            self.xy = x, y
```
**变异语义**：_AnnotationBase 对 `isinstance(xy, np.ndarray)` 特判直接 `self.xy = xy`（存引用），非数组才解包复制。恰好对数组类型漏复制——而 bug 正是数组场景。事后改 an_xy 改 annotation。F2P 失败。保留。

### Group C — 保留（D1 状态：直接存引用）
```diff
-        x, y = xy  # Make copy when xy is an array (and check the shape).
-        self.xy = x, y
+        self.xy = xy
```
**变异语义**：_AnnotationBase 的解包复制改成直接 `self.xy = xy`——存原引用、不复制，还原原 bug。外部修改 xy 数组改 annotation。F2P 失败。保留。

### Group D — 新增（D1 状态：OffsetFrom 解包但存原引用）
```diff
         x, y = ref_coord  # Make copy when ref_coord is an array (and check the shape).
-        self._ref_coord = x, y
+        self._ref_coord = ref_coord
```
**变异语义**：OffsetFrom 保留解包行 `x, y = ref_coord`（做了 shape 检查）但 `self._ref_coord = ref_coord`（仍存原数组引用）——解包白做了。比 A（连解包行一起删）隐蔽：解包语句还在、只是存错了对象。事后改数组改 annotation。F2P 失败。新增为 D。

### Group E — 保留（E2 隐式→显式开关）
```diff
-                 annotation_clip=None):
+                 annotation_clip=None,
+                 _copy_input=False):
...
-        x, y = xy  # Make copy when xy is an array (and check the shape).
-        self.xy = x, y
+        if _copy_input:
+            x, y = xy  # Make copy ...
+            self.xy = x, y
+        else:
+            self.xy = xy
```
**变异语义**：_AnnotationBase 的 xy 复制藏到 `_copy_input` 参数后（默认 False）——默认直接 `self.xy = xy`（存引用），外部修改改 annotation。只有显式开启才复制。默认即 bug。F2P 失败。保留。

## 新设计 Mutation 说明

原始 C==D 字节相同（都 `self.xy = xy`），缺 A，且 B/C/D/E 都只覆盖 _AnnotationBase 路径。本次保留 B（ndarray 特判跳过）、C（直接存引用）、E（_copy_input 默认关闭开关）于 _AnnotationBase，新增 A（OffsetFrom 不解包）、D（OffsetFrom 解包但存原引用）于 OffsetFrom 路径——覆盖 F2P 的 OffsetFrom 子用例。五组覆盖"OffsetFrom 不解包 / ndarray 特判 / 直接存引用 / OffsetFrom 解包存原引用 / 默认关闭开关"五个角度，跨两个 __init__——全部令外部修改数组改 annotation。全部实测（Python 3.9/matplotlib 3.7.2，源码构建 C 扩展，conda 编译器，LTO 禁用）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
