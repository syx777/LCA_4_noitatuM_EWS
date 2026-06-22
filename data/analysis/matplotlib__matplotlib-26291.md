# matplotlib__matplotlib-26291

## 问题背景

用 `inset_axes` 创建 inset 时，配合 `savefig(bbox_inches="tight")` 报错（gh-26287）。根因：`AnchoredLocatorBase.__call__(ax, renderer)` 在 tight bbox 计算时 renderer 可能为 None，后续 `get_window_extent(renderer)` 崩溃。Golden patch 在 `__call__` 开头加 `if renderer is None: renderer = ax.figure._get_renderer()`。

## Golden Patch 语义分析

```python
def __call__(self, ax, renderer):
    if renderer is None:
        renderer = ax.figure._get_renderer()
    self.axes = ax
    bbox = self.get_window_extent(renderer)
    ...
```
核心语义：**renderer 为 None 时，从 `ax.figure._get_renderer()` 取一个真实 renderer 兜底**。关键点：`if renderer is None` 判断、`ax.figure._get_renderer()` 正确取 renderer、赋值给 renderer 供后续使用。

F2P 测试 `test_axes_grid1.py::test_inset_axes_tight`：`inset_axes(ax, ...)` 后 `fig.savefig(BytesIO, bbox_inches="tight")` 不报错。

## 调用链分析

`savefig(bbox_inches="tight")` → 计算 tight bbox 时以 renderer=None 调 `AnchoredLocatorBase.__call__(ax, None)` → golden 的 fallback 取 renderer → `get_window_extent(renderer)`。fallback 条件反转、赋错值、删除、取错方法、或门控开关，都会让 None renderer 进 get_window_extent 崩溃。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `if renderer is None`→`is not None`，条件反转 |
| B | 🟢 高质量 | 重做 | fallback 赋 `renderer = None`，无效兜底 |
| C | 🟢 高质量 | 保留 | 删除整个 fallback 块（还原 bug） |
| D | 🟢 高质量 | 重做 | `ax.figure._get_renderer()`→`ax.get_renderer()` 错方法 |
| E | 🟢 高质量 | 保留 | fallback 藏到 allow_renderer_fallback 开关后 |

原始 A==B（`is not None`）、C==D（删 fallback）。保留 A、C、E，重做 B（赋 None 无效）、D（取错方法）。

## 各组 Mutation 分析

### Group A — 保留（B3 条件反转）
```diff
-        if renderer is None:
+        if renderer is not None:
             renderer = ax.figure._get_renderer()
```
**变异语义**：`if renderer is None` 反转成 `is not None`——renderer 为 None（tight bbox 时）不进入 fallback、仍是 None，`get_window_extent(None)` 崩溃；renderer 非 None（已有）时反而被覆盖。条件颠倒。F2P 失败。保留。

### Group B — 重做（C1 值：赋 None 无效）
**原**：与 A 相同（`is not None`）。
**最终 mutation**：
```diff
         if renderer is None:
-            renderer = ax.figure._get_renderer()
+            renderer = None
```
**变异语义**：fallback 里 `renderer = ax.figure._get_renderer()` 改成 `renderer = None`——None 时进入分支却仍赋 None，`get_window_extent(None)` 崩溃。fallback 形同无效（自赋 None）。比 A（条件反转）不同——条件对、兜底值错。F2P 失败。重做为 B。

### Group C — 保留（B2 删 fallback）
```diff
-        if renderer is None:
-            renderer = ax.figure._get_renderer()
         self.axes = ax
```
**变异语义**：删除整个 `if renderer is None: renderer = ...` fallback 块——还原原 bug：renderer 为 None 时不补救，`savefig(bbox_inches='tight')` 时 None 进 get_window_extent 崩溃。F2P 失败。保留。

### Group D — 重做（A1 接口契约：取错方法）
**原**：与 C 相同（删 fallback）。
**最终 mutation**：
```diff
         if renderer is None:
-            renderer = ax.figure._get_renderer()
+            renderer = ax.get_renderer()
```
**变异语义**：fallback 里 `ax.figure._get_renderer()` 改成 `ax.get_renderer()`——Axes 对象没有 `get_renderer` 方法，AttributeError。取错了对象的方法（应从 figure 取）。比 C（删 fallback）隐蔽——fallback 还在、只是调用错。F2P 失败。重做为 D。

### Group E — 保留（E2 隐式→显式开关）
```diff
-                 borderpad=0.5, bbox_transform=None):
+                 borderpad=0.5, bbox_transform=None, allow_renderer_fallback=False):
...
+        self.allow_renderer_fallback = allow_renderer_fallback
...
-        if renderer is None:
+        if renderer is None and self.allow_renderer_fallback:
             renderer = ax.figure._get_renderer()
```
**变异语义**：renderer fallback 藏到 `AnchoredLocatorBase(allow_renderer_fallback=False)` 参数后（默认 False）——默认 None renderer 不补救，savefig tight 崩溃。只有显式开启才 fallback。模拟"把 renderer 兜底做成可配置、默认却关掉"。F2P 失败。保留。

## 新设计 Mutation 说明

原始 A==B（`is not None`）、C==D（删 fallback），实际只有"条件反转 / 删 fallback"两种机制。本次保留 A（条件反转）、C（删 fallback 还原 bug）、E（allow_renderer_fallback 默认关闭开关），重做 B（fallback 自赋 None、无效兜底）、D（`ax.get_renderer()` 取错对象方法）。五组覆盖"条件反转 / 无效兜底 / 删 fallback / 取错方法 / 默认关闭开关"五个角度——全部令 None renderer 进 get_window_extent 崩溃。全部实测（Python 3.9/matplotlib 3.7.2，源码构建 C 扩展，conda 编译器，LTO 禁用）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
