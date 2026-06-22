# matplotlib__matplotlib-23299

## 问题背景

`get_backend()` 会清空 `Gcf.figs` 中在 `rc_context` 内创建的 figure。根因：`rc_context` 上下文管理器退出时会恢复进入前的 rcParams 快照，其中包含 `backend` 项；恢复 backend 会触发 backend 重新选择，导致 `Gcf` 里的 figure 被清除。Golden patch 在保存快照时把 `backend` 从 `orig` 中删除（`orig = dict(rcParams.copy()); del orig['backend']`），使 rc_context 不再恢复/重置 backend。

## Golden Patch 语义分析

```python
orig = dict(rcParams.copy())
del orig['backend']
try:
    ...
finally:
    dict.update(rcParams, orig)
```
核心语义：**rc_context 保存的 rcParams 快照必须先转成普通 dict 再 `del orig['backend']`，使退出恢复时不触碰 backend**——否则恢复 backend 触发 backend 重选、清空 Gcf。关键点：转成普通 `dict`（脱离 RcParams 的校验/默认行为）、`del` 真正移除 backend 键、无条件执行。

F2P 测试 `test_rcparams.py::test_no_backend_reset_rccontext`：在 `rc_context` 内设 `rcParams['backend']='module://aardvark'`，退出后断言该 backend 仍保持（即未被快照恢复重置）。

## 调用链分析

`rc_context()` → `orig = dict(rcParams.copy())` → `del orig['backend']` → yield → finally `dict.update(rcParams, orig)`。若 backend 留在 orig，退出时恢复它触发 backend 重选；F2P 在 context 内改 backend、退出后检查是否保持。del 被改成读取/注释/类型错/条件化都会让 backend 被恢复。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `del orig['backend']`→`orig.get('backend')`，只读不删 |
| B | 🟢 高质量 | 保留 | `if fname`→`if not fname`，流程条件反转 |
| C | 🟢 高质量 | 保留 | `dict(rcParams.copy())`→`rcParams.copy()`，类型用错 |
| D | 🟢 高质量 | 保留 | 注释掉 `del orig['backend']` |
| E | 🟢 高质量 | 保留 | del 藏到 reset_backend 开关后 |

五组机制各异，全部保留并核验。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：只读不删）
```diff
-    del orig['backend']
+    orig.get('backend')
```
**变异语义**：`del orig['backend']`（删除）换成 `orig.get('backend')`（只读取、丢弃返回值）。backend 仍留在 orig 快照中，rc_context 退出时恢复它 → 触发 backend 重选 → 清空 Gcf 里 rc_context 内创建的 figure。还原原 bug。F2P 失败。保留。

### Group B — 保留（B3 条件反转）
```diff
-        if fname:
+        if not fname:
             rc_file(fname)
```
**变异语义**：`if fname` 反转成 `if not fname`。无 fname（常见）时反而调 `rc_file(None)` → 抛错或异常行为；有 fname 时不加载。rc_context 流程被破坏。F2P 失败。保留。

### Group C — 保留（C1 类型：RcParams vs dict）
```diff
-    orig = dict(rcParams.copy())
+    orig = rcParams.copy()
```
**变异语义**：去掉外层 `dict(...)`，`orig` 仍是 `RcParams` 实例（带校验逻辑与 backend 默认处理）。在 RcParams 上 `del orig['backend']` 的行为与普通 dict 不同——可能不真正移除、或在 `dict.update` 恢复时 backend 仍被带回/重置。golden 特意转成普通 dict 正是为绕开这点。F2P 失败。保留。

### Group D — 保留（B2 注释掉删除）
```diff
-    del orig['backend']
+    # del orig['backend']
```
**变异语义**：注释掉 `del orig['backend']`。backend 留在 orig，退出恢复触发 backend 重选、清空 figure。与 A（改成只读）效果相同但形式是注释。还原原 bug。F2P 失败。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
-def rc_context(rc=None, fname=None):
+def rc_context(rc=None, fname=None, reset_backend=True):
...
-    del orig['backend']
+    if not reset_backend:
+        del orig['backend']
```
**变异语义**：del backend 藏到 `reset_backend` 参数后，仅 `if not reset_backend` 才删。参数默认 `True` → 默认**不**删 backend（`not True` 为假）→ backend 被恢复、清 figure。参数名误导性地暗示"重置 backend"，默认行为即原 bug。F2P 失败。保留。

## 新设计 Mutation 说明

本实例原五组机制已各不相同且全部有效，无重复，故全部保留并逐一核验。五组覆盖"只读不删 / 流程条件反转 / RcParams 类型 / 注释删除 / 默认开关"五个角度，分别作用于删除语句、fname 分支、快照类型、删除存在性、特性开关五个环节——全部令 backend 被 rc_context 恢复、清空 Gcf 内 figure。全部实测（Python 3.9/matplotlib 3.5.2，源码构建 C 扩展）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
