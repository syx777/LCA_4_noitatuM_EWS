# matplotlib__matplotlib-20826

## 问题背景

`ax.clear()` 在共享轴（`sharex/sharey`）场景下会多出刻度、并让本应隐藏的共享轴 tick label 重新显现（3.4.1→3.4.2 的行为退化）。根因：`Axis.clear()` 调用 `self._reset_major_tick_kw()` / `_reset_minor_tick_kw()` 把 tick kw 字典整体重置成默认值——这抹掉了共享轴预存的 tick label 可见性等状态。Golden patch 改为不整体重置，只更新 `gridOn` 这一项（根据 rcParams 计算），保留字典里其它已设置的 kw。

## Golden Patch 语义分析

```python
# whether the grids are on
self._major_tick_kw['gridOn'] = (
        mpl.rcParams['axes.grid'] and
        mpl.rcParams['axes.grid.which'] in ('both', 'major'))
self._minor_tick_kw['gridOn'] = (
        mpl.rcParams['axes.grid'] and
        mpl.rcParams['axes.grid.which'] in ('both', 'minor'))
self.reset_ticks()
```
核心语义：**`clear()` 不应整体重置 tick kw 字典（`_reset_*_tick_kw` 会丢失共享轴预存的可见性状态），而应原地只更新 `gridOn` 键**——保留字典里其它 kw（如 label 可见性）。关键点：用 `self._major_tick_kw['gridOn'] = ...` 原地赋值（不 clear、不重新绑定字典、不调 reset），gridOn 值用 `and` 连接 `axes.grid` 与 `which in ('both','major'/'minor')`。

F2P 测试 `test_axes.py::test_shared_axes_clear`（check_figures_equal）：对 2x2 共享轴子图，参考图直接 plot、测试图先 `ax.clear()` 再 plot，断言两者渲染一致（即 clear 不应多出刻度/标签）。

## 调用链分析

`ax.clear()` → 各 `Axis.clear()` → 设置 gridOn（golden）或 `_reset_*_tick_kw()`（bug）→ `reset_ticks()`。tick kw 字典里存着共享轴的 tick label 可见性等，若被 reset/clear/重新绑定就丢失，导致 clear 后隐藏标签重现、多余刻度出现。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 新增 | 新增 | 把 tick_kw 重新绑定为新字典字面量，丢预存 kw |
| B | 🟢 高质量 | 保留 | 前插 `_reset_major_tick_kw()`，重置 major kw |
| C | 🟢 高质量 | 保留 | `.clear()` 两字典 + `and`→`or` |
| D | 🟢 高质量 | 保留 | `.clear()` 两字典（原地清空） |
| E | 🟢 高质量 | 保留 | 还原原 bug：`_reset_major/minor_tick_kw()` |

原始 B/C/D/E 中，E 还原 bug、B 前插 reset、C clear+or、D clear。缺 A。新增 A（重新绑定新字典，与 D 的原地 clear 区分）。

## 各组 Mutation 分析

### Group A — 新增（D1 状态：重新绑定字典）
```diff
-        self._major_tick_kw['gridOn'] = (... 'major'))
-        self._minor_tick_kw['gridOn'] = (... 'minor'))
+        self._major_tick_kw = {'gridOn': (... 'major'))}
+        self._minor_tick_kw = {'gridOn': (... 'minor'))}
```
**变异语义**：把 `self._major_tick_kw['gridOn'] = ...`（原地更新键）改成 `self._major_tick_kw = {'gridOn': ...}`（重新绑定为只含 gridOn 的新字典）。新字典丢弃了此前预存的其它 tick kw（共享轴的 tick label 可见性等），ax.clear() 后隐藏标签重现。与 D（原地 `.clear()`）效果相近但机制不同：换了字典对象。F2P 失败。新增为 A。

### Group B — 保留（D1 状态：前插 reset major）
```diff
         # whether the grids are on
+        self._reset_major_tick_kw()
         self._major_tick_kw['gridOn'] = (...)
```
**变异语义**：在设置 gridOn 前插入 `self._reset_major_tick_kw()`——它把 major tick kw 整体重置成默认（含可见性），随后虽设了 gridOn，但其它预存状态已丢。还原 clear 后多余刻度/标签的 bug。F2P 失败。保留。

### Group C — 保留（B3 逻辑+状态：clear + and→or）
```diff
+        self._major_tick_kw.clear()
         self._major_tick_kw['gridOn'] = (
-                mpl.rcParams['axes.grid'] and
+                mpl.rcParams['axes.grid'] or
                 ...'major'))
+        self._minor_tick_kw.clear()
         ... or ...'minor'))
```
**变异语义**：两个 tick_kw 先 `.clear()`（抹掉预存可见性），再设 gridOn，且把 `and`→`or`（gridOn 判定逻辑也错——axes.grid 为真即恒真）。双重破坏：状态丢失 + 逻辑错。F2P 失败。保留。

### Group D — 保留（D1 状态：clear 两字典）
```diff
+        self._major_tick_kw.clear()
         self._major_tick_kw['gridOn'] = (...)
+        self._minor_tick_kw.clear()
         self._minor_tick_kw['gridOn'] = (...)
```
**变异语义**：在设 gridOn 前对两个 tick_kw 调 `.clear()` 原地清空，抹掉预存 tick 可见性等 kw。与 A（重新绑定新字典）效果相近，机制是原地清空同一字典对象。F2P 失败。保留。

### Group E — 保留（A1 接口契约：还原 reset）
```diff
-        # whether the grids are on
-        self._major_tick_kw['gridOn'] = (...)
-        self._minor_tick_kw['gridOn'] = (...)
+        self._reset_major_tick_kw()
+        self._reset_minor_tick_kw()
```
**变异语义**：还原原始 bug——用 `_reset_major_tick_kw()`/`_reset_minor_tick_kw()` 替换 golden 的直接设 gridOn。reset 重建默认 tick kw、丢失共享轴隐藏状态，clear 后刻度/标签错误显现。F2P 失败。保留。

## 新设计 Mutation 说明

原始有 B/C/D/E、缺 A，且 B/D/E 都围绕"重置/清空 tick kw"。本次保留 B（前插 reset major）、C（clear+and→or）、D（clear 两字典）、E（还原 _reset_* bug），新增 A（重新绑定字典字面量，与 D 的原地 clear 区分）。五组覆盖"重新绑定字典 / 前插 reset / clear+逻辑错 / clear 两字典 / 还原 reset"五个角度——全部令 clear() 丢失共享轴预存 tick 状态。全部实测（Python 3.9/matplotlib 3.4.2，源码构建 C 扩展）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
