# matplotlib__matplotlib-25775

## 问题背景

Text 对象的抗锯齿（antialiased）状态只能从全局 `rcParams["text.antialiased"]` 读取，不能像其它 artist 那样按对象配置。Golden patch 给 Text 加 `get_antialiased`/`set_antialiased` 和 `antialiased` 构造参数，并在 `draw` 时把 `self._antialiased` 通过 GraphicsContext 传给后端（backend_agg/backend_cairo 用 `gc.get_antialiased()`）。

## Golden Patch 语义分析

```python
def __init__(self, ..., antialiased=None, **kwargs):
    self._antialiased = mpl.rcParams['text.antialiased']
    self._reset_visual_defaults(..., antialiased=antialiased)

def _reset_visual_defaults(self, ..., antialiased=None):
    ...
    if antialiased is not None:
        self.set_antialiased(antialiased)

def set_antialiased(self, antialiased):
    self._antialiased = antialiased; self.stale = True
def get_antialiased(self):
    return self._antialiased
```
核心语义：**Text 需有独立 `_antialiased` 状态：默认初始化为 rcParams 值；构造时若显式传 antialiased（非 None）才覆盖；get/set 读写该状态**。关键点：`_antialiased` 初始化为 rcParams、`if antialiased is not None` 守卫（None 时保留 rcParams 默认）、get_antialiased 返回实例状态。

F2P 测试 `test_text.py::test_set_antialiased`/`test_get_antialiased`/`test_annotation_antialiased`：断言 set/get 往返、构造传值生效、未传时 == rcParams。

## 调用链分析

`Text(antialiased=True)` → `__init__` 设 `_antialiased=rcParams` → `_reset_visual_defaults(antialiased=True)` → `if not None: set_antialiased(True)`。`get_antialiased()` 返回 `_antialiased`。默认值、守卫、初始化、get 逻辑出错都会让 antialiased 状态不符。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | antialiased 默认 None→False |
| B | 🟢 高质量 | 保留 | `if antialiased is not None`→`is None` |
| C | 🟢 高质量 | 保留 | 删守卫直接 set(antialiased) |
| D | 🟢 高质量 | 保留 | 删 `_antialiased=rcParams` 初始化 |
| E | 🟢 高质量 | 保留 | get_antialiased 加 use_rcparams 默认返 rcParams |

五组机制各异，全部保留并核验。

## 各组 Mutation 分析

### Group A — 保留（C1 值：默认 False）
```diff
-                 antialiased=None,  # defaults to rcParams['text.antialiased']
+                 antialiased=False,  # defaults to rcParams['text.antialiased']
```
**变异语义**：Text.__init__ 的 antialiased 默认值 None→False。`if antialiased is not None` 守卫对 False 恒真 → 未传 antialiased 的 Text 被 `set_antialiased(False)`，而非保留 rcParams 默认。`test_get_antialiased` 的 txt4（期望 ==rcParams）失败。保留。

### Group B — 保留（B3 条件反转）
```diff
-        if antialiased is not None:
+        if antialiased is None:
             self.set_antialiased(antialiased)
```
**变异语义**：守卫 `is not None` 反转成 `is None`——传了 antialiased（非 None）时反而不 set（漏设、保留 rcParams 默认）、没传（None）时 set(None)。`test_get_antialiased`（txt2/txt3 期望 True/False）失败。保留。

### Group C — 保留（B2 删守卫）
```diff
-        if antialiased is not None:
-            self.set_antialiased(antialiased)
+        self.set_antialiased(antialiased)
```
**变异语义**：去掉 `if antialiased is not None` 守卫直接 `set_antialiased(antialiased)`——未传时 antialiased=None 被 set 成 `_antialiased=None`（而非保留 rcParams 默认）。`test`（未传时期望 ==rcParams）失败。保留。

### Group D — 保留（D1 状态：删初始化）
```diff
         self._text = ''
-        self._antialiased = mpl.rcParams['text.antialiased']
```
**变异语义**：删除 __init__ 里 `self._antialiased = mpl.rcParams['text.antialiased']` 初始化。未传 antialiased 时（守卫跳过 set）`_antialiased` 属性从未被设置，`get_antialiased()` 访问时 AttributeError 或缺省。`test_get_antialiased`（txt4）失败。保留。

### Group E — 保留（A2 接口契约：get 返 rcParams）
```diff
-    def get_antialiased(self):
+    def get_antialiased(self, use_rcparams=True):
+        if use_rcparams:
+            import matplotlib as mpl
+            return mpl.rcParams['text.antialiased']
         return self._antialiased
```
**变异语义**：get_antialiased 加 `use_rcparams=True` 参数，默认返回 `rcParams['text.antialiased']` 而非 `self._antialiased`。显式设了 antialiased 的 Text，get_antialiased() 仍返回全局 rcParams 值。`test_get_antialiased`（断言 get == self._antialiased）失败。模拟"getter 默认走全局而非实例状态"。保留。

## 新设计 Mutation 说明

本实例原五组机制已各不相同且全部有效，无重复，故全部保留并逐一核验。五组覆盖"默认值 / 守卫反转 / 删守卫 / 删初始化 / getter 默认走 rcParams"五个角度，分别作用于构造默认、reset 守卫、守卫存在性、属性初始化、getter 逻辑五个环节——全部令 Text 的 antialiased 状态读写不符。全部实测（Python 3.9/matplotlib 3.7.1，源码构建 C 扩展，conda 编译器）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
