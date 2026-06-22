# matplotlib__matplotlib-23476

## 问题背景

figure 反序列化（unpickle）后 dpi 翻倍（M1 Mac，循环可致 OverflowError）。根因：在高 device pixel ratio 屏幕上，`_set_device_pixel_ratio` 会把 `_dpi` 乘以 ratio，但 figure 同时保存了未缩放的 `_original_dpi`。pickle 时 `__getstate__` 直接存了被缩放的 `_dpi`，unpickle 后又叠加一次缩放 → 翻倍。Golden patch 在 `__getstate__` 里把 `state["_dpi"]` 恢复成 `_original_dpi`（若有），丢弃 pixel-ratio 引起的缩放。

## Golden Patch 语义分析

```python
def __getstate__(self):
    ...
    # discard any changes to the dpi due to pixel ratio changes
    state["_dpi"] = state.get('_original_dpi', state['_dpi'])
    ...
```
核心语义：**pickle 时存入的 dpi 应回退到未缩放的 `_original_dpi`（若存在），否则用当前 `_dpi`**——丢弃 device pixel ratio 带来的缩放，避免 unpickle 后重复缩放。关键点：`state.get('_original_dpi', state['_dpi'])`——key 是 `_original_dpi`、默认回退到 `_dpi`、结果赋给 `state["_dpi"]`。

F2P 测试 `test_figure.py::test_unpickle_with_device_pixel_ratio`：`Figure(dpi=42)` 后 `_set_device_pixel_ratio(7)` 使 dpi=294，pickle→unpickle 后断言 `fig2.dpi == 42`（回到原始）。

## 调用链分析

`pickle.dumps(fig)` → `Figure.__getstate__` → `state["_dpi"] = state.get('_original_dpi', state['_dpi'])` → 存盘。`pickle.loads` → `__setstate__` 恢复 _dpi。若 __getstate__ 存的是缩放后的 _dpi（不回退 original），unpickle 后 dpi 仍翻倍。取错 key、不赋值、条件错、门控开关都会让翻倍 dpi 被存。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `state.get('_original_dpi', _dpi)`→`state['_dpi']`，不回退 |
| B | 🟢 高质量 | 保留 | 仅当 `_original_dpi > _dpi` 才恢复，方向错 |
| C | ➕ 补充 | 重做 | `get('_dpi', _original_dpi)`，取错 key 恒得翻倍值 |
| D | ➕ 补充 | 重做 | `state.pop('_original_dpi')`，只删不赋值 |
| E | 🟢 高质量 | 保留 | 恢复藏到 `_preserve_original_dpi` 开关后 |

原始 A==C==D（都使 _dpi 保留翻倍值），实际只有"不回退"一种机制。保留 A（直接用 _dpi）、B（条件方向错）、E（开关），重做 C（取错 key）、D（pop 不赋值）。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：不回退 original）
```diff
-        state["_dpi"] = state.get('_original_dpi', state['_dpi'])
+        state["_dpi"] = state['_dpi']
```
**变异语义**：直接存当前（已被 device_pixel_ratio 翻倍的）`_dpi`，不回退到 `_original_dpi`。unpickle 后 dpi 仍是翻倍值。还原原 bug。F2P 断言 42 实得 294 → 失败。保留。

### Group B — 保留（B3 条件方向错）
```diff
-        state["_dpi"] = state.get('_original_dpi', state['_dpi'])
+        if '_original_dpi' in state and state['_original_dpi'] > state['_dpi']:
+            state["_dpi"] = state['_original_dpi']
```
**变异语义**：只在 `_original_dpi > _dpi` 时才恢复 original。但翻倍场景 `_dpi`（缩放后=294）> `_original_dpi`（42），条件为假 → 不恢复，dpi 保留翻倍值。条件方向写反（应是 `<` 或无条件）。F2P 失败。保留。

### Group C — 重做（C1 值：取错 key）
**原**：与 A 相同（`state['_dpi']`）。
**最终 mutation**：
```diff
-        state["_dpi"] = state.get('_original_dpi', state['_dpi'])
+        state["_dpi"] = state.get('_dpi', state['_original_dpi'])
```
**变异语义**：`state.get('_original_dpi', state['_dpi'])` 的 key 与默认对调成 `state.get('_dpi', state['_original_dpi'])`。`_dpi` 键一定存在（值为翻倍 294）→ get 恒返回翻倍值，`_original_dpi` 沦为永不触发的默认。比 A 隐蔽——看着还在 get _original_dpi 相关。F2P 失败。重做为 C。

### Group D — 重做（D1 状态：pop 不赋值）
**原**：与 A 相同。
**最终 mutation**：
```diff
-        state["_dpi"] = state.get('_original_dpi', state['_dpi'])
+        state.pop('_original_dpi', None)
```
**变异语义**：改成 `state.pop('_original_dpi', None)`——只是从 state 移除 `_original_dpi` 键，**根本不给 `state['_dpi']` 赋值**。存盘的 `_dpi` 仍是翻倍值。模拟"误把'恢复 dpi'写成了'清理 original_dpi 键'"。F2P 失败。重做为 D。

### Group E — 保留（E2 隐式→显式开关）
```diff
-        state["_dpi"] = state.get('_original_dpi', state['_dpi'])
+        if getattr(self, "_preserve_original_dpi", False):
+            state["_dpi"] = state.get('_original_dpi', state['_dpi'])
```
**变异语义**：dpi 恢复藏到 `_preserve_original_dpi` 开关后（默认 False）。默认 unpickle 不恢复 original dpi，翻倍值被保留。只有显式开启才恢复。模拟"把 dpi 恢复做成可配置、默认却关掉"。F2P 失败。保留。

## 新设计 Mutation 说明

原始 A==C==D 字节/语义等价（都使 `_dpi` 保留翻倍值），实际只有"不回退"一种机制。本次保留 A（直接用 _dpi）、B（条件方向错）、E（_preserve_original_dpi 默认关闭开关），重做 C（key 与默认对调、取错 key 恒得翻倍值）、D（pop 只删不赋值）。五组覆盖"不回退 / 条件方向错 / 取错 key / pop 不赋值 / 默认关闭开关"五个角度——全部令 unpickle 后 dpi 保留翻倍值。全部实测（Python 3.9/matplotlib 3.5.2，源码构建 C 扩展）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
