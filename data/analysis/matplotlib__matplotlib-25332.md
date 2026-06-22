# matplotlib__matplotlib-25332

## 问题背景

调用 `align_labels()` 后的 figure 无法 pickle（`TypeError: cannot pickle 'weakref.ReferenceType' object`）。根因：`cbook.Grouper`（用于 `_align_label_groups`）的 `_mapping` 用 weakref 作 key/value，weakref 不可 pickle。Golden patch 给 `Grouper` 加 `__getstate__`（pickle 时把 weakref 解引用成强引用）和 `__setstate__`（unpickle 时把强引用重新包成 weakref）。

## Golden Patch 语义分析

```python
def __getstate__(self):
    return {
        **vars(self),
        "_mapping": {k(): [v() for v in vs] for k, vs in self._mapping.items()},
    }
def __setstate__(self, state):
    vars(self).update(state)
    self._mapping = {weakref.ref(k): [*map(weakref.ref, vs)]
                     for k, vs in self._mapping.items()}
```
核心语义：**`__getstate__` 把 `_mapping` 的 weakref key/value 解引用（`k()`、`v()`）成强引用以便 pickle；`__setstate__` 反向把强引用重新包成 `weakref.ref`**。关键点：getstate 里 key `k()`、value `[v() for v in vs]` 都解引用；setstate 里 key `weakref.ref(k)`、value `[*map(weakref.ref, vs)]` 都重新弱化。

F2P 测试 `test_pickle.py::test_complete`（含 `fig.align_ylabels()`）：pickle figure 后能 loads。

## 调用链分析

`align_ylabels()` → 填充 `_align_label_groups` 的 Grouper（weakref mapping）→ `pickle.dumps(fig)` → Grouper.`__getstate__` 解引用 → pickle 强引用 → `loads` → `__setstate__` 重新弱化。getstate key/value 漏解引用、setstate 过滤/漏弱化、或门控开关，都会让 pickle 报 weakref 错或状态损坏。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | getstate key `k()`→`k`，key 漏解引用 |
| B | 🟢 高质量 | 保留 | setstate 加 `if k is None`，mapping 被清空 |
| C | 🟢 高质量 | 保留 | getstate value `[v()...]`→`vs`，value 漏解引用 |
| D | 🟢 高质量 | 保留 | setstate value `[*map(ref,vs)]`→`vs`，漏弱化 |
| E | 🟢 高质量 | 保留 | 转换藏到 _convert_refs_on_pickle 开关后 |

五组机制各异，全部保留并核验。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：key 漏解引用）
```diff
-            "_mapping": {k(): [v() for v in vs] for k, vs in self._mapping.items()},
+            "_mapping": {k: [v() for v in vs] for k, vs in self._mapping.items()},
```
**变异语义**：`__getstate__` 里 key `k()`（解引用）改成 `k`（保留 weakref 对象）。字典 key 仍是 weakref，pickle 时 weakref 不可序列化，`pickle.dumps` 仍抛 'cannot pickle weakref'。F2P 失败。保留。

### Group B — 保留（B2 状态：清空 mapping）
```diff
         self._mapping = {weakref.ref(k): [*map(weakref.ref, vs)]
-                         for k, vs in self._mapping.items()}
+                         for k, vs in self._mapping.items() if k is None}
```
**变异语义**：`__setstate__` 重建 mapping 时加 `if k is None` 过滤——实际 k 都是真实对象（非 None），全被过滤，`_mapping` 变空字典。unpickle 后 Grouper 丢失所有分组信息，align 状态损坏（虽不崩溃但语义错，后续断言/行为不符）。F2P 失败。保留。

### Group C — 保留（C1 值：value 漏解引用）
```diff
-            "_mapping": {k(): [v() for v in vs] for k, vs in self._mapping.items()},
+            "_mapping": {k: vs for k, vs in self._mapping.items()},
```
**变异语义**：`__getstate__` 里 key 和 value 都不解引用（`k: vs`）——key 保留 weakref、value 保留 weakref 列表。pickle 时 weakref 不可序列化，dumps 报错。比 A（仅 key）更彻底。F2P 失败。保留。

### Group D — 保留（C1 值：setstate 漏弱化）
```diff
-        self._mapping = {weakref.ref(k): [*map(weakref.ref, vs)]
+        self._mapping = {weakref.ref(k): vs
                          for k, vs in self._mapping.items()}
```
**变异语义**：`__setstate__` 重建时 value `[*map(weakref.ref, vs)]`（重新弱化）改成 `vs`（保留强引用）——value 不重新包成 weakref。Grouper 内部假设 _mapping 的 value 是 weakref 列表，后续 weakref 操作（如 `.join`/`__contains__` 的解引用）出错或语义不符。F2P 失败。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
-    def __getstate__(self):
-        return {
-            **vars(self),
-            "_mapping": {k(): [v() for v in vs] for k, vs in self._mapping.items()},
-        }
+    def __getstate__(self):
+        if getattr(self, "_convert_refs_on_pickle", False):
+            return {... "_mapping": {k(): [v() for v in vs] ...} }
+        return vars(self)
```
**变异语义**：weakref→strong 转换藏到 `_convert_refs_on_pickle` 开关后（默认 False）——默认 `__getstate__` 直接返回含 weakref 的 `vars(self)`，pickle 报 weakref 不可序列化。只有显式开启才转换。默认即 bug。F2P 失败。保留。

## 新设计 Mutation 说明

本实例原五组机制已各不相同且全部有效，无重复，故全部保留并逐一核验。五组覆盖"getstate key 漏解引用 / setstate 过滤清空 / getstate value 漏解引用 / setstate 漏弱化 / 默认关闭开关"五个角度，分别作用于 getstate 的 key、setstate 的过滤、getstate 的 value、setstate 的 value、特性开关五个环节——全部令 pickle 报 weakref 错或 unpickle 状态损坏。全部实测（Python 3.9/matplotlib 3.7.0，源码构建 C 扩展，conda 编译器，contourpy 1.0.7）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
