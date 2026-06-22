# matplotlib__matplotlib-25311

## 问题背景

带可拖拽 legend 的 figure 无法 pickle（`TypeError: cannot pickle 'FigureCanvasQTAgg' object`）。根因：`DraggableBase.__init__` 把 `self.canvas = self.ref_artist.figure.canvas` 存为实例属性，pickle figure 时会连带 pickle 这个 GUI canvas（不可序列化）。Golden patch 删除该实例属性，改用 `canvas = property(lambda self: self.ref_artist.figure.canvas)`——property 不进 `__dict__`，pickle 时不带 canvas。

## Golden Patch 语义分析

```python
def __init__(self, ref_artist, use_blit=False):
    ...
    self.got_artist = False
    # (removed: self.canvas = self.ref_artist.figure.canvas)
    self._use_blit = use_blit and self.canvas.supports_blit
    ...

# A property, not an attribute, to maintain picklability.
canvas = property(lambda self: self.ref_artist.figure.canvas)
```
核心语义：**canvas 必须是 property（每次动态从 `ref_artist.figure.canvas` 取），而非实例属性**——这样 pickle DraggableBase（随 figure）时 canvas 不进 state，避免 pickle 不可序列化的 GUI canvas。关键点：删除 `self.canvas=` 赋值、用 property 定义 canvas、不在 __getstate__ 重新引入 canvas。

F2P 测试 `test_pickle.py::test_complete`（含 `legend(draggable=True)`）：pickle figure 后断言 pickle 流中无 `FigureCanvasAgg` 引用，且能 loads + canvas.draw()。

## 调用链分析

`pickle.dumps(fig)` → 递归 pickle DraggableLegend(DraggableBase) → 若 canvas 在 `__dict__`（实例属性）则被 pickle（GUI canvas 不可序列化 / Agg canvas 留下引用）。golden 用 property 使 canvas 不在 __dict__。__getstate__ 引入 canvas、删 property、property 构造新对象、加回实例属性、或门控开关，都会让 canvas 进 pickle 流。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | __getstate__ 把 canvas 塞进 state |
| B | 🟢 高质量 | 保留 | 删 property，self.canvas 未定义 |
| C | 🟢 高质量 | 保留 | property 返回新构造 canvas 副本 |
| D | 🟢 高质量 | 保留 | 加回实例属性 + 删 property（原 bug） |
| E | 🟢 高质量 | 保留 | canvas 缓存藏到 _cache_canvas 开关后 |

五组机制各异，全部保留并核验。

## 各组 Mutation 分析

### Group A — 保留（D2 状态：__getstate__ 引入 canvas）
```diff
     canvas = property(lambda self: self.ref_artist.figure.canvas)
+
+    def __getstate__(self):
+        state = self.__dict__.copy()
+        state['canvas'] = self.canvas
+        return state
```
**变异语义**：给 DraggableBase 加 `__getstate__` 把 canvas 显式塞进 pickle state——即使 canvas 是 property，pickle 时 state 含 canvas 引用，F2P 断言 pickle 流无 FigureCanvasAgg 失败（或 GUI canvas dump 报错）。绕过了 property 的保护。保留。

### Group B — 保留（B2 删 property）
```diff
-    # A property, not an attribute, to maintain picklability.
-    canvas = property(lambda self: self.ref_artist.figure.canvas)
```
**变异语义**：删除 `canvas = property(...)`。golden 删了 __init__ 里的 `self.canvas=` 赋值、改用 property；删掉 property 后 `self.canvas`（在 __init__ 的 `_use_blit` 处引用）AttributeError，draggable 构造直接崩溃。F2P 失败。保留。

### Group C — 保留（C1 值：构造新 canvas）
```diff
-    canvas = property(lambda self: self.ref_artist.figure.canvas)
+    canvas = property(lambda self: type(self.ref_artist.figure.canvas)(self.ref_artist.figure.canvas))
```
**变异语义**：property 改成返回 `type(canvas)(canvas)`（用 canvas 类构造新对象）——每次访问构造新 canvas 副本，语义错（应返回 figure 的 canvas 本身），且新对象可能持有不可 pickle 引用或构造失败。F2P 失败。保留。

### Group D — 保留（D1 状态：加回实例属性）
```diff
+        self.canvas = self.ref_artist.figure.canvas
         self._use_blit = use_blit and self.canvas.supports_blit
...
-    # A property, not an attribute, to maintain picklability.
-    canvas = property(lambda self: self.ref_artist.figure.canvas)
```
**变异语义**：在 __init__ 加回 `self.canvas = self.ref_artist.figure.canvas` 实例属性并删除 property——还原原 bug：canvas 作为实例属性进 `__dict__`，pickle 时带上不可序列化的 GUI canvas（或 Agg canvas 引用）。F2P 失败。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
-    def __init__(self, ref_artist, use_blit=False):
+    def __init__(self, ref_artist, use_blit=False, _cache_canvas=True):
...
+        if _cache_canvas:
+            self.canvas = self.ref_artist.figure.canvas
...
-    canvas = property(lambda self: self.ref_artist.figure.canvas)
+    canvas = property(lambda self: self.__dict__.get('canvas') or self.ref_artist.figure.canvas)
```
**变异语义**：canvas 缓存藏到 `_cache_canvas=True` 参数后（默认 True）——默认把 canvas 存为实例属性，pickle 流含 canvas 引用。property 也优先返回缓存的实例属性。只有显式关闭才纯用动态 property。默认即 bug。F2P 失败。保留。

## 新设计 Mutation 说明

本实例原五组机制已各不相同且全部有效，无重复，故全部保留并逐一核验。五组覆盖"__getstate__ 引入 canvas / 删 property 崩溃 / 构造新 canvas / 加回实例属性 / 默认缓存开关"五个角度，分别作用于 pickle 状态、property 定义、property 返回值、__init__ 属性、特性开关五个环节——全部令 canvas 进 pickle 流或构造崩溃。全部实测（Python 3.9/matplotlib 3.7.0，源码构建 C 扩展，conda 编译器，contourpy 1.0.7）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
