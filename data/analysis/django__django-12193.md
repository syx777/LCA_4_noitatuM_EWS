# django__django-12193

## 问题背景

`SplitArrayField` 搭配 `BooleanField` 时，渲染多个 `CheckboxInput` 子组件出现"checked 状态一旦出现就不再消失"的 bug。根本原因在于 `CheckboxInput.get_context()` 直接修改（mutate）了传入的 `attrs` 字典：

```python
# base_commit 中的 bug 代码
if attrs is None:
    attrs = {}
attrs['checked'] = True  # 原地修改！
```

`SplitArrayWidget.get_context()` 在 for 循环中复用同一个 `final_attrs` 字典，当第 i 个 checkbox（value=True）把 `final_attrs['checked'] = True` 写入后，后续所有 checkbox 都继承了这个 `checked` 标志，即使它们对应的值是 False。

## Golden Patch 语义分析

修复的核心是：**不再原地修改传入的 attrs 字典，而是创建一个新字典**：

```python
# golden fix
attrs = {**(attrs or {}), 'checked': True}
```

这一行同时处理了：
1. `attrs` 为 None 的情况（`attrs or {}`）
2. 不修改调用方传入的字典（创建新对象）

修复使 `CheckboxInput.get_context()` 对调用方的 `attrs` 参数无副作用（pure w.r.t. input），遵循了"函数不应修改传入参数"的契约。

## 调用链分析

```
SplitArrayWidget.get_context(name, value=[True, False], attrs=None)
  ├─ final_attrs = self.build_attrs(attrs)    # 创建共享字典 {}
  └─ for i in range(size):
       └─ self.widget.get_context(name_i, widget_value, final_attrs)
            └─ CheckboxInput.get_context(name, value, attrs=final_attrs)
                 ├─ [BUG] attrs['checked'] = True  ← 修改了 final_attrs!
                 └─ super().get_context(name, value, attrs)
                      └─ Widget.get_context()
                           └─ build_attrs(self.attrs, attrs)  → 最终渲染 attrs
```

数据流：`final_attrs` 是被所有迭代共享的同一个字典对象。base_commit 中，任何迭代将 `checked: True` 写入它后，该状态在后续迭代中永久存在。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 与 base_commit bug 代码完全相同，是 golden patch 的直接逆操作 |
| B | 🔴 必须替换 | 替换 | `attrs['checked'] = True` 跳过 None 检查，同样是直接还原 bug 行为 |
| C | 🔴 必须替换 | 替换 | 与 Group A diff 完全相同（同一 git index hash），重复冗余 |
| D | 🟡 语义浅层 | 保留 | 条件分支逻辑有一定深度，能在 `attrs` 已含 `checked` 时暴露 bug |
| E | 新增 | 新增 | 为缺失的第五组设计跨函数高质量 mutation |

语义浅层共 0 个（A/B/C 均为必须替换），无需额外语义浅层替换决策。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：
```diff
-            attrs = {**(attrs or {}), 'checked': True}
+            if attrs is None:
+                attrs = {}
+            attrs['checked'] = True
```
**分类**：🔴 必须替换  
**理由**：与 base_commit 原始 bug 代码逐字相同，是 golden patch 的完整逆操作。代码审查者一眼即可看出这是在还原 bug。  
**最终 mutation**：
```diff
     def get_context(self, name, value, attrs):
         if self.check_test(value):
+            self.attrs['checked'] = True
             attrs = {**(attrs or {}), 'checked': True}
         return super().get_context(name, value, attrs)
```
**变异语义**：在正确创建新 attrs 字典的同时，还将 `checked: True` 写入 widget 实例的 `self.attrs`。首次 True 渲染后，`self.attrs = {'checked': True}` 永久存在。当 `SplitArrayWidget` 在同一循环内复用同一 widget 实例时，False 值的 checkbox 渲染时虽然不进 if 分支，但 `Widget.get_context` 中 `build_attrs(self.attrs={'checked':True}, extra_attrs={})` 仍会产生 `{'checked': True}`，导致 `test_checkbox_get_context_attrs` 失败。`test_get_context_does_not_mutate_attrs` 因只检查传入 `attrs` 参数本身不被修改而通过。难以发现：代码看起来像"同时更新实例状态和返回值"，是合理的编程意图。

### Group B — 替换
**原 mutation**：
```diff
-            attrs = {**(attrs or {}), 'checked': True}
+            attrs['checked'] = True
```
**分类**：🔴 必须替换  
**理由**：去掉了 None 检查和新字典创建，直接原地修改，如果 `attrs=None` 还会崩溃。效果与 Group A 基本等价，只是更简陋。  
**最终 mutation**：
```diff
     def get_context(self, name, value, attrs):
         if self.check_test(value):
+            if attrs:
+                attrs['checked'] = True
             attrs = {**(attrs or {}), 'checked': True}
         return super().get_context(name, value, attrs)
```
**变异语义**：当 `attrs` 为 truthy（非空非 None）时，先原地修改它（`attrs['checked'] = True`），再创建新字典。`test_get_context_does_not_mutate_attrs` 传入 `attrs={'checked': False}`（truthy），先被原地修改为 `{'checked': True}`，测试断言 `attrs['checked'] is False` 失败。现有渲染测试均传入 `attrs=None`，绕过了 `if attrs:` 判断，全部通过。难以发现：看起来像"只在有现有 attrs 时才先标记，避免覆盖 None 的边界处理"。

### Group C — 替换
**原 mutation**：与 Group A 完全相同（git index hash `6fe220bea7` 相同）  
**分类**：🔴 必须替换（冗余重复）  
**理由**：与 Group A diff 内容完全一致，无任何区分度。  
**最终 mutation**：
```diff
     def get_context(self, name, value, attrs):
         if self.check_test(value):
-            attrs = {**(attrs or {}), 'checked': True}
+            attrs = attrs or {}
+            attrs['checked'] = True
+            attrs = dict(attrs)
         return super().get_context(name, value, attrs)
```
**变异语义**：`attrs = attrs or {}` 保留原始引用（若 attrs 非空则等于同一对象），`attrs['checked'] = True` 原地修改该引用，`attrs = dict(attrs)` 制作副本返回。关键：`attrs or {}` 当 `attrs = {'checked': False}` 时返回 `{'checked': False}`（同一引用），原地修改直接污染调用方的字典。`test_get_context_does_not_mutate_attrs` 和 `test_checkbox_get_context_attrs` 均失败。难以发现：`attrs = dict(attrs)` 最后复制一份的写法令人误以为原始 attrs 没有被污染。

### Group D — 保留
**原 mutation**：
```diff
-            attrs = {**(attrs or {}), 'checked': True}
+            if 'checked' not in (attrs or {}):
+                attrs = {**(attrs or {}), 'checked': True}
+            else:
+                attrs['checked'] = True
```
**分类**：🟡 语义浅层（保留）  
**理由**：修改位置位于关键控制流，分支逻辑有一定语义深度——当 `checked` 已存在时走 else 原地修改，对传入 `attrs = {'checked': False}` 的 F2P 测试会失败；逻辑看似合理（"存在则更新，不存在则创建新字典"），不易一眼识破。  
**最终 mutation**（与原相同）：
```diff
            if 'checked' not in (attrs or {}):
                attrs = {**(attrs or {}), 'checked': True}
            else:
                attrs['checked'] = True
```
**变异语义**：当 `attrs` 中已含 `checked` 键时（无论值为何），走 else 原地修改 `attrs['checked'] = True`，污染调用方字典。`test_get_context_does_not_mutate_attrs`（传入 `{'checked': False}`）命中 else 分支，直接失败。对于 `test_checkbox_get_context_attrs` 中的 `final_attrs = {}`，第一次（True）走 if 新建字典，第二次（False）不进 `if self.check_test(value)` 分支，不触发此代码，`final_attrs` 未被污染，反而通过。

### Group E — 新增
**设计理由**：前四组 mutation 均集中在 `CheckboxInput.get_context` 方法。为提升覆盖广度，Group E 在 `Widget.build_attrs` 中引入跨函数副作用：计算结果正确，但同时将 `extra_attrs` 的内容合并回 `base_attrs`（即 `self.attrs`），造成 widget 实例 attrs 的累积污染。  
**最终 mutation**：
```diff
     def build_attrs(self, base_attrs, extra_attrs=None):
         """Build an attribute dictionary."""
-        return {**base_attrs, **(extra_attrs or {})}
+        result = {**base_attrs, **(extra_attrs or {})}
+        base_attrs.update(extra_attrs or {})
+        return result
```
**变异语义**：每次调用 `build_attrs` 都将 `extra_attrs` 写回 `base_attrs`（即 `self.attrs`）。`Widget.get_context` 调用 `build_attrs(self.attrs, new_attrs)` 时，`self.attrs` 被 `new_attrs` 污染。首次 True 渲染后，`CheckboxInput.self.attrs = {'checked': True}`；后续 False 渲染虽不进 check_test 分支，但 `build_attrs({'checked': True}, {})` 仍返回 `{'checked': True}`，导致 `test_checkbox_get_context_attrs` 失败。`test_get_context_does_not_mutate_attrs` 因检测的是传入 `attrs` 参数（不是 `self.attrs`）而通过。难以发现：`result` 先正确计算，`base_attrs.update(...)` 看似"同步更新基础状态"，是 stateful widget 设计模式下容易犯的错误。

## 新设计 Mutation 说明

**Group A 新设计**：基于对 `Widget.get_context → build_attrs(self.attrs, attrs)` 调用链的分析。`self.attrs` 是 widget 实例级别的持久状态，而 `attrs` 是调用时传入的临时参数。在 `SplitArrayWidget` 多次调用同一 widget 实例时，任何对 `self.attrs` 的修改都会跨 `get_context` 调用持久化。真实开发者可能会误认为"需要同时更新实例状态和返回值"。

**Group B 新设计**：基于对"attrs 非空时直接操作，为空时新建"的分支逻辑分析。开发者可能认为"如果已经有一个 attrs 字典，直接在上面加标记更高效"，而不意识到这个字典可能是被调用方共享的 `final_attrs`。

**Group C 新设计**：基于对 `attrs = attrs or {}` 惯用法的误解分析。开发者可能认为这行代码已经"复制了"attrs（类似 `attrs = list(attrs)`），而实际上 `or` 运算符只在 falsy 时替换，非空字典直接返回原引用。最后的 `dict(attrs)` 复制看起来像是在"做正确的事"，掩盖了中间的污染。

**Group E 新设计**：基于对 `build_attrs` 在整个 Widget 类层次中的角色分析。作为所有 widget 渲染的基础方法，`build_attrs` 的任何副作用都会向上传播。将 `extra_attrs` 同步回 `base_attrs` 是一种"让基础状态跟上最新合并结果"的开发者直觉，但破坏了 immutable-input 契约。
