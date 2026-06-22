# matplotlib__matplotlib-21568

## 问题背景

datetime 轴在 `usetex=True` 时间距渲染不清晰（3.4 相比 3.3 退化）。`_wrap_in_tex` 把日期/时间标签包进 TeX `\mathdefault{}` 时，未对冒号 `:` 和空格做特殊处理，导致连字符、冒号被当作二元运算符渲染出多余间距、数字间空格被吞。Golden patch 在 `_wrap_in_tex` 中：把 `-`→`{-}`、`:`→`{:}`（花括号包裹避免二元运算符间距），并把空格 ` `→`\;`（TeX 细空格，避免数字间距被吞）。

## Golden Patch 语义分析

```python
def _wrap_in_tex(text):
    p = r'([a-zA-Z]+)'
    ret_text = re.sub(p, r'}$\1$\mathdefault{', text)
    # Braces ensure symbols are not spaced like binary operators.
    ret_text = ret_text.replace('-', '{-}').replace(':', '{:}')
    # To not concatenate space between numbers.
    ret_text = ret_text.replace(' ', r'\;')
    ret_text = '$\mathdefault{' + ret_text + '}$'
    ret_text = ret_text.replace('$\mathdefault{}$', '')
    return ret_text
```
核心语义：**三个符号替换缺一不可——`-`→`{-}`、`:`→`{:}`（花括号避免破折号/冒号被当二元运算符）、` `→`\;`（普通空格转 TeX 细空格保留数字间距）**。每个替换对应一类 usetex 标签的正确渲染：日期破折号、时间冒号、数字间空格。

F2P 测试 `test_dates.py::test_date_formatter_usetex`（参数化多种时间跨度）：断言 usetex 下日期/时间标签的 TeX 串形如 `$\mathdefault{1990{-}01{-}%02d}$`、`$\mathdefault{01\;00{:}%02d}$` 等（含 `{-}`/`{:}`/`\;`）。

## 调用链分析

`DateFormatter`/`ConciseDateFormatter` 在 usetex 下对每个 tick 标签调 `_wrap_in_tex(text)` → 三次 `replace` 处理符号 → 包进 `$\mathdefault{...}$`。任一 replace 被删/改无效/注释/藏到开关后，对应符号（破折号/冒号/空格）的 TeX 处理就失效，标签串与期望不符。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 去掉 `.replace(':', '{:}')`，冒号不包裹 |
| B | 🟢 高质量 | 重做 | 去掉 `.replace('-', '{-}')`，破折号不包裹 |
| C | ➕ 新增 | 新增 | 空格替换 `r'\;'`→`' '`（替换成普通空格、无效） |
| D | 🟢 高质量 | 保留 | 注释掉空格替换行 |
| E | 🟢 高质量 | 保留 | 符号替换藏到 `replace_symbols` 开关后 |

原始有 A/B/D/E，缺 C；且原 B 改的是 `ConciseDateFormatter` 的 `level >= 5`（不被本 F2P 触发，实测不破坏）。保留 A（丢冒号）、D（注释空格）、E（开关），重做 B（丢破折号，与 A 对称），新增 C（空格替换无效）。

## 各组 Mutation 分析

### Group A — 保留（C1 值：丢冒号替换）
```diff
-    ret_text = ret_text.replace('-', '{-}').replace(':', '{:}')
+    ret_text = ret_text.replace('-', '{-}')
```
**变异语义**：去掉链式的 `.replace(':', '{:}')`，冒号不再用花括号包裹。usetex 下时间标签（如 `00:00`）的冒号被当二元运算符渲染、间距错，TeX 串缺 `{:}` → 与期望 `00{:}00` 不符。F2P 失败。保留。

### Group B — 重做（C1 值：丢破折号替换）
**原**：改 `ConciseDateFormatter` 的 `level >= 5`→`> 5`（不被 `test_date_formatter_usetex` 触发，实测 rc=0 不破坏）。
**最终 mutation**：
```diff
-    ret_text = ret_text.replace('-', '{-}').replace(':', '{:}')
+    ret_text = ret_text.replace(':', '{:}')
```
**变异语义**：去掉 `.replace('-', '{-}')`（只留冒号替换），破折号不被花括号包裹。日期标签（如 `1990-01-01`）的破折号被当二元运算符、间距错，TeX 串缺 `{-}`。与 A 对称（A 丢冒号、B 丢破折号），都作用于 `_wrap_in_tex`（确保被 F2P 触发）。重做为 B。

### Group C — 新增（C1 值：空格替换无效）
```diff
-    ret_text = ret_text.replace(' ', r'\;')
+    ret_text = ret_text.replace(' ', ' ')
```
**变异语义**：空格替换的目标从 `r'\;'`（TeX 细空格）改成 `' '`（普通空格，等于没替换）。数字间空格不转成 TeX 细空格，标签（如 `01 00:00`）的空格渲染被吞/间距错，TeX 串缺 `\;`。模拟"replace 目标写成原值、替换形同无效"。F2P 失败。新增为 C。

### Group D — 保留（B2 注释掉替换）
```diff
     # To not concatenate space between numbers.
-    ret_text = ret_text.replace(' ', r'\;')
+    # ret_text = ret_text.replace(' ', r'\;')
```
**变异语义**：把空格替换整行注释掉，空格转 TeX 细空格的逻辑失效。与 C（替换成普通空格）效果相同但机制不同：直接删该行。标签空格未处理。F2P 失败。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
-def _wrap_in_tex(text):
+def _wrap_in_tex(text, replace_symbols=False):
...
-    ret_text = ret_text.replace('-', '{-}').replace(':', '{:}')
-    # To not concatenate space between numbers.
-    ret_text = ret_text.replace(' ', r'\;')
+    if replace_symbols:
+        ret_text = ret_text.replace('-', '{-}').replace(':', '{:}')
+        # To not concatenate space between numbers.
+        ret_text = ret_text.replace(' ', r'\;')
```
**变异语义**：三个符号替换整体藏到 `replace_symbols` 参数后（默认 False），默认 `_wrap_in_tex` 不做任何符号替换，usetex 标签全部退回未处理形式（无 `{-}`/`{:}`/`\;`），还原原 bug。调用方都不传该参数。模拟"把符号 TeX 化做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

原始有 A/B/D/E、缺 C，且原 B 误改了 `ConciseDateFormatter.format_ticks` 的 `level >= 5`——该路径不被 F2P `test_date_formatter_usetex` 触发（实测 rc=0 不破坏）。本次保留 A（丢冒号替换）、D（注释空格替换）、E（replace_symbols 默认关闭开关），重做 B（丢破折号替换，与 A 对称且确保命中 `_wrap_in_tex`），新增 C（空格替换成普通空格、无效）。五组覆盖"丢冒号 / 丢破折号 / 空格替换无效 / 注释空格替换 / 默认关闭开关"五个角度，全部作用于 `_wrap_in_tex` 的符号处理。全部实测（Python 3.9/matplotlib 3.4.2，源码构建 C 扩展）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
