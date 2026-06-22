# matplotlib__matplotlib-26342

## 问题背景

`ContourSet` 需要 `set_paths` 方法（Cartopy 等用 `cs.get_paths()[:] = transformed_paths` 的 workaround 不优雅）。Golden patch 在基类 `Collection.set_paths` 实现真正的赋值（`self._paths = paths; self.stale = True`，替换原来的 `raise NotImplementedError`），并删除 `PathCollection` 里重复的 `set_paths` 覆盖（让它继承基类的）。

## Golden Patch 语义分析

```python
# Collection
def set_paths(self, paths):
    self._paths = paths
    self.stale = True

# PathCollection: (removed its own set_paths override)
```
核心语义：**基类 `Collection.set_paths` 应真正赋值 `self._paths = paths` 并标记 stale，而非 `raise NotImplementedError`；并删除 PathCollection 的重复覆盖**。关键点：`self._paths = paths` 直接赋值、`self.stale = True`、且 `_paths` 在 `__init__` 已初始化为 None。

F2P 测试 `test_contour.py::test_contour_set_paths`（check_figures_equal）：`cs_test.set_paths(cs_ref.get_paths())`，断言 test 图（用 ref 的 paths 替换）与 ref 图一致。

## 调用链分析

`cs.set_paths(paths)` → `Collection.set_paths` → `self._paths = paths` → 绘制时 `get_paths()` 返回新 paths。set_paths 存错属性、加错误守卫、不赋值、_paths 未初始化、或门控开关，都会让 set_paths 不生效，test 图与 ref 不一致。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 存到 `self._pending_paths`（错属性名） |
| B | 🟢 高质量 | 保留 | 加 `if paths is None` 守卫，正常调用失效 |
| C | 🟢 高质量 | 保留 | 注释掉 `self._paths = paths` |
| D | 🟢 高质量 | 保留 | __init__ 注释掉 `self._paths = None` 初始化 |
| E | 🟢 高质量 | 保留 | 赋值藏到 set_paths(apply=False) 开关后 |

五组机制各异，全部保留并核验。

## 各组 Mutation 分析

### Group A — 保留（D1 状态：错属性名）
```diff
     def set_paths(self, paths):
-        self._paths = paths
+        # Store paths in a temporary variable instead of directly assigning
+        self._pending_paths = paths
         self.stale = True
```
**变异语义**：set_paths 把 `self._paths = paths` 改成 `self._pending_paths = paths`——存到了错误的属性名，`get_paths()` 返回的 `_paths` 未更新，set_paths 形同无效。`test_contour_set_paths`（set_paths 后图应等于 ref）失败。保留。

### Group B — 保留（B3 条件：None 守卫）
```diff
     def set_paths(self, paths):
-        self._paths = paths
-        self.stale = True
+        if paths is None:
+            self._paths = paths
+            self.stale = True
```
**变异语义**：set_paths 加 `if paths is None` 守卫——只有 paths 为 None 才赋值，传入真实 paths 列表时不更新 _paths。正常调用（传 paths）失效。F2P 失败。保留。

### Group C — 保留（B2 注释赋值）
```diff
     def set_paths(self, paths):
-        self._paths = paths
+        # self._paths = paths
         self.stale = True
```
**变异语义**：注释掉 `self._paths = paths`（只留 `self.stale = True`）——paths 不被存储，set_paths 无效。F2P 失败。保留。

### Group D — 保留（D1 状态：删初始化）
```diff
         self._internal_update(kwargs)
-        self._paths = None
+        # self._paths = None
```
**变异语义**：Collection.__init__ 注释掉 `self._paths = None` 初始化——`_paths` 属性未初始化，绘制/get_paths 链路上访问 `_paths` 时 AttributeError 或状态异常。删初始化。F2P 失败。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
-    def set_paths(self, paths):
-        self._paths = paths
-        self.stale = True
+    def set_paths(self, paths, apply=False):
+        if apply:
+            self._paths = paths
+            self.stale = True
```
**变异语义**：set_paths 赋值藏到 `apply=False` 参数后——默认 apply=False 故不赋值 `_paths`，`set_paths(ref_paths)`（调用方不传 apply）无效。只有显式 apply=True 才生效。默认即 bug。F2P 失败。保留。

## 新设计 Mutation 说明

本实例原五组机制已各不相同且全部有效，无重复，故全部保留并逐一核验。五组覆盖"错属性名 / None 守卫 / 注释赋值 / 删初始化 / 默认关闭开关"五个角度，分别作用于 set_paths 的目标属性、守卫、赋值存在性、__init__ 初始化、特性开关五个环节——全部令 set_paths 不生效。全部实测（Python 3.9/matplotlib 3.7.2，源码构建 C 扩展，conda 编译器，LTO 禁用）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
