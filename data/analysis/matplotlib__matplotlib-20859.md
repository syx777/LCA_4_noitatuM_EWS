# matplotlib__matplotlib-20859

## 问题背景

给 `SubFigure` 添加 legend 失败。`subfig.legend()` 抛 `TypeError: Legend needs either Axes or Figure as parent`。根因：`Legend.__init__` 用 `isinstance(parent, Figure)` 校验 parent，而 `SubFigure` 不是 `Figure` 的子类（两者都继承自 `FigureBase`）。Golden patch 把校验改成 `isinstance(parent, FigureBase)`（并相应改 import 和错误信息），使 SubFigure/Figure 都被接受。

## Golden Patch 语义分析

```python
from matplotlib.figure import FigureBase
...
elif isinstance(parent, FigureBase):
    self.isaxes = False
    self.set_figure(parent)
else:
    raise TypeError("Legend needs either Axes or FigureBase as parent")
```
核心语义：**Legend 的 parent 校验应针对 `FigureBase`（Figure 和 SubFigure 的共同基类）而非 `Figure`**——这样 SubFigure 也被接受。关键点：import 的是 FigureBase、isinstance 检查 FigureBase、且为肯定式（`isinstance` 不是 `not isinstance`）。

F2P 测试 `test_legend.py::test_subfigure_legend`：`plt.figure().subfigures()` 得 SubFigure，`subfig.legend()` 应成功且 `leg.figure is subfig`。

## 调用链分析

`SubFigure.legend()` → `Legend(self, ...)` → `__init__` 中 `isinstance(parent, FigureBase)` 分支 → `set_figure(parent)`。若检查的是 Figure（SubFigure 不匹配）、或类型错、或取反、或藏开关，则 SubFigure 落到 else 抛 TypeError。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `FigureBase`→`Figure`（+import），还原原 bug |
| B | 🟢 高质量 | 保留 | `FigureBase`→`Axes`，figure 永不匹配 |
| C | 🟢 高质量 | 重做 | import 别名 `Figure as FigureBase`，名义 FigureBase 实为 Figure |
| D | ➕ 新增 | 新增 | `isinstance`→`not isinstance`，判断取反 |
| E | ➕ 新增 | 新增 | FigureBase 分支加默认关闭开关 |

原始 A/B/C 中 A 与 C 功能等价（都让检查实际针对 Figure）。保留 A（直接改 Figure）、B（改 Axes），重做 C（import 别名，隐蔽），新增 D（取反）、E（开关）。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：FigureBase→Figure）
```diff
-        from matplotlib.figure import FigureBase
+        from matplotlib.figure import Figure, FigureBase
...
-        elif isinstance(parent, FigureBase):
+        elif isinstance(parent, Figure):
```
**变异语义**：还原原始 bug——isinstance 检查 `Figure`。SubFigure 不是 Figure 子类，`subfig.legend()` 不匹配该分支、落到 else 抛 TypeError。F2P 失败。保留。

### Group B — 保留（C1 类型：FigureBase→Axes）
```diff
-        elif isinstance(parent, FigureBase):
+        elif isinstance(parent, Axes):
```
**变异语义**：isinstance 类型改成 `Axes`。任何 figure 类 parent（Figure/SubFigure）都不匹配该分支（且前一个 if 已处理 Axes），落到 else 抛 TypeError。模拟"类型名写成了另一个无关类"。F2P 失败。保留。

### Group C — 重做（A1 接口契约：import 别名）
**原**：补 `from matplotlib.figure import Figure` 并把检查改成 `isinstance(parent, Figure)`——与 A 功能等价。
**最终 mutation**：
```diff
-        from matplotlib.figure import FigureBase
+        from matplotlib.figure import Figure as FigureBase
```
**变异语义**：import 用别名 `Figure as FigureBase`——代码里 `isinstance(parent, FigureBase)` 行不变，但 `FigureBase` 这个名字实际绑定到 `Figure`。于是检查实质是 `isinstance(parent, Figure)`，SubFigure 不匹配。比 A 隐蔽：检查行看着用的是 FigureBase（正确），错在 import。F2P 失败。重做为 C。

### Group D — 新增（B3 条件反转：not isinstance）
```diff
-        elif isinstance(parent, FigureBase):
+        elif not isinstance(parent, FigureBase):
```
**变异语义**：isinstance 判断取反。figure 类 parent（本应匹配）反而不匹配、落到 else 抛 TypeError；非 figure 的 parent 反而进该分支 `set_figure`。逻辑颠倒。`subfig.legend()` 报错。F2P 失败。新增为 D。

### Group E — 新增（E2 隐式→显式开关）
```diff
-        elif isinstance(parent, FigureBase):
+        elif isinstance(parent, FigureBase) and getattr(self, '_allow_subfigure', False):
```
**变异语义**：FigureBase 分支追加 `and getattr(self, '_allow_subfigure', False)` 开关（默认 False）。默认情况下 figure/subfigure parent 都不进该分支（开关假）、落到 else 抛 TypeError。只有显式开启才接受。模拟"把 FigureBase 支持做成可配置、默认却关掉"。F2P 失败。新增为 E。

## 新设计 Mutation 说明

原始仅 A/B/C，且 A 与 C 功能等价（都使检查实际针对 Figure），缺 D/E。本次保留 A（直接改 Figure 还原 bug）、B（改 Axes 类型），重做 C（import 别名 `Figure as FigureBase`，比 A 隐蔽），新增 D（`not isinstance` 取反）、E（`_allow_subfigure` 默认关闭开关）。五组覆盖"改 Figure / 改 Axes / import 别名 / 判断取反 / 默认关闭开关"五个角度——全部令 SubFigure parent 不被接受、抛 TypeError。全部实测（Python 3.9/matplotlib 3.4.2，源码构建 C 扩展）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
