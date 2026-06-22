# matplotlib__matplotlib-25479

## 问题背景

colormap 名称处理混乱：用不同于 colormap 内部 name 的名字注册（`register(name='wrong-cmap', cmap=cmap)` 而 cmap.name='test-cmap'）后，`get_cmap('wrong-cmap')` 返回的 cmap 名仍是旧的，且因 `__eq__` 比较 name 导致查找/相等判断异常。Golden patch 两处：(1) `cm.py` register 后把注册的 cmap.name 更新成注册名；(2) `colors.py` Colormap.`__eq__` 去掉 `self.name != other.name` 判断（名字不同但查找表相同应判等）。

## Golden Patch 语义分析

```python
# cm.py register
self._cmaps[name] = cmap.copy()
if self._cmaps[name].name != name:
    self._cmaps[name].name = name

# colors.py __eq__
def __eq__(self, other):
    if (not isinstance(other, Colormap) or
            self.colorbar_extend != other.colorbar_extend):
        return False
    ...
    return np.array_equal(self._lut, other._lut)
```
核心语义：**(1) 注册时把 cmap 的 name 同步成注册名；(2) `__eq__` 不再比较 name（只比 colorbar_extend 和 lookup table）**。关键点：cm.py 的 name 更新逻辑、colors.py 去掉 name 比较。

F2P 测试 `test_colors.py::test_colormap_equals`（改名后同表 colormap 应相等）+ `test_set_cmap_mismatched_name`（注册后 cmap.name 应等于注册名）。

## 调用链分析

`colormaps.register(name='wrong-cmap', cmap)` → `_cmaps[name]=cmap.copy()` → name 同步。`get_cmap('wrong-cmap')` 返回该 cmap，断言其 name=='wrong-cmap'。`cm_copy==cmap`（改名后）→ `__eq__` 不比 name → 相等。name 更新被注释/反转/删除、或 __eq__ 加回 name 比较、或门控开关，都会让 F2P 断言失败。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 注释掉 cm.py name 更新逻辑 |
| B | 🟢 高质量 | 保留 | name 更新条件 `!=`→`==` |
| C | 🟢 高质量 | 保留 | 删除 cm.py name 更新块 |
| D | 🔴 必须替换 | 替换 | 原 D 与 A 字节相同；改为 colors.py __eq__ 加回 name 比较 |
| E | 🟢 高质量 | 保留 | name 更新藏到 update_name 开关后 |

原始 A==D 字节相同（都注释 cm.py name 更新）。保留 A、B、C、E，重做 D 为 colors.py `__eq__` 加回 name 比较（golden 的另一处修改）。

## 各组 Mutation 分析

### Group A — 保留（B2 注释 name 更新）
```diff
-        if self._cmaps[name].name != name:
-            self._cmaps[name].name = name
+        # if self._cmaps[name].name != name:
+        #     self._cmaps[name].name = name
```
**变异语义**：注释掉 cm.py register 里的 name 更新逻辑——注册后 colormap 对象仍保留旧的 builtin/内部名，`get_cmap('wrong-cmap').name != 'wrong-cmap'`。F2P 的 `test_set_cmap_mismatched_name`（断言 name=='wrong-cmap'）失败。保留。

### Group B — 保留（B3 条件反转）
```diff
-        if self._cmaps[name].name != name:
+        if self._cmaps[name].name == name:
             self._cmaps[name].name = name
```
**变异语义**：name 更新条件 `!= name` 改成 `== name`——只有名字已相同时才"更新"（无意义的 no-op），名字不同时反而不更新。注册后 cmap 名仍是旧的。F2P 失败。保留。

### Group C — 保留（B2 删除 name 更新块）
```diff
-        if self._cmaps[name].name != name:
-            self._cmaps[name].name = name
```
**变异语义**：直接删除整个 name 更新块（注释行也一并删）——同 A 效果，注册后 cmap 名不更新。与 A（注释）形式不同：彻底删除。F2P 失败。保留。

### Group D — 替换（A1 接口契约：__eq__ 加回 name 比较）
**原**：与 A 字节相同（注释 cm.py name 更新）。
**最终 mutation**（作用于 `colors.py`）：
```diff
-        if (not isinstance(other, Colormap) or
+        if (not isinstance(other, Colormap) or self.name != other.name or
                 self.colorbar_extend != other.colorbar_extend):
```
**变异语义**：在 colors.py 的 `Colormap.__eq__` 里加回 `self.name != other.name` 判断——还原原 bug：名字不同的同表 colormap 被判不等。F2P 的 `test_colormap_equals`（改名后 `cm_copy == cmap` 应为真）断言失败。这是 golden 的**另一处**修改（不同文件），与 A/B/C（cm.py）形成互补。重做为 D。

### Group E — 保留（E2 隐式→显式开关）
```diff
-    def register(self, cmap, *, name=None, force=False):
+    def register(self, cmap, *, name=None, force=False, update_name=False):
...
-        if self._cmaps[name].name != name:
+        if update_name and self._cmaps[name].name != name:
             self._cmaps[name].name = name
```
**变异语义**：name 更新藏到 `register(update_name=False)` 参数后（默认 False）——默认注册不更新 cmap 名，`get_cmap` 返回旧名。只有显式开启才更新。默认即 bug。F2P 失败。保留。

## 新设计 Mutation 说明

原始 A==D 字节相同（都注释 cm.py 的 name 更新逻辑）。本次保留 A（注释 name 更新）、B（条件 `!=`→`==`）、C（删除 name 更新块）、E（update_name 默认关闭开关），把与 A 重复的 D 重做为 colors.py `__eq__` 加回 `self.name != other.name` 比较——针对 golden 的另一处修改（不同文件），覆盖 `test_colormap_equals` 断言。五组覆盖"注释 name 更新 / 条件反转 / 删除块 / __eq__ 加 name 比较 / 默认关闭开关"五个角度，跨 cm.py 与 colors.py 两文件——全部令 colormap 名称/相等处理回到 bug 行为。全部实测（Python 3.9/matplotlib 3.7.1，源码构建 C 扩展，conda 编译器，contourpy 1.0.7）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
