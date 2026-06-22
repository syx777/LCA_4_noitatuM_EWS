# django__django-16485

## 问题背景

`floatformat("0.00", 0)` 和 `floatformat(Decimal("0.00"), 0)` 崩溃：`ValueError: valid range for prec is [1, MAX_PREC]`。当值为零（`m`，即 `int(d)-d`，为 0）且精度参数 `p` 为 0 时，原代码 `if not m and p < 0` 不进入"整数快捷格式化"分支，继续走 `Decimal` 量化路径，最终 `Context(prec=...)` 算出的 prec 为 0（非法，Decimal 要求 prec ≥ 1）而报错。Golden patch 把条件从 `p < 0` 改成 `p <= 0`，使 `p == 0` 的零值也走快捷分支、直接返回 `"0"`，绕开非法 prec。

## Golden Patch 语义分析

```python
if not m and p <= 0:        # 原为 p < 0
    return mark_safe(
        formats.number_format("%d" % (int(d)), 0, use_l10n=use_l10n, force_grouping=force_grouping)
    )
```
核心语义：**当值无小数部分（`not m`）且请求精度 `p <= 0` 时，应直接按整数格式化返回，而不进入会算出非法 prec 的 Decimal 量化路径**。原 `p < 0` 漏掉了 `p == 0` 的边界：`floatformat("0.00", 0)` 中 m=0、p=0，`not m and 0<0` 为假 → 继续走量化 → prec=0 → ValueError。改成 `<= 0` 把 p=0 也纳入快捷分支。这是经典的边界 off-by-one（`<` 应为 `<=`）。

F2P 测试 `FunctionTests.test_zero_values` 新增 `floatformat("0.00", 0) == "0"` 与 `floatformat(Decimal("0.00"), 0) == "0"`，断言零值 + 精度 0 不崩溃、返回 `"0"`。

## 调用链分析

`floatformat(text, arg)`：解析 `p = int(arg)`，算 `m = int(d) - d`（值的小数部分，零值时 m=0）。`if not m and p < 0` 命中则走整数 `number_format` 快捷返回；否则继续 `exp = Decimal(1).scaleb(-abs(p))`、算 `prec` 并 `d.quantize(exp, ..., Context(prec=prec))`。当 m=0、p=0 时漏入量化路径，`prec` 计算得 0，`Context(prec=0)` 非法。修复让 p=0 的零值走快捷分支。条件的边界（`<` vs `<=`）、`not m` 项、判断变量都影响是否正确短路。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `p <= 0`→`p < 0`，还原 off-by-one bug |
| B | 🔴 必须替换 | 替换 | 原 B 与 A 字节相同；改为 `p <= -1`（同样排除 0，不同字面量） |
| C | 🔴 必须替换 | 替换 | 原 C 与 A 字节相同；改为 `if not m and p`（真值测试，p=0 为假被排除） |
| D | 🔴 必须替换 | 替换 | 原 D 与 A 字节相同；改为 `if m and p <= 0`（要求 m 真值，零值 m=0 被排除） |
| E | 🟢 高质量 | 保留 | 快捷分支藏到默认关闭的 `strict_precision` 开关后 |

原 A、B、C、D 字节完全相同（`p <= 0`→`p < 0`）。保留 A、E，重做 B、C、D 为不同的"把 p=0 零值排除出快捷分支"的机制。本 patch 仅一行条件，五组围绕该条件分化。

## 各组 Mutation 分析

### Group A — 保留（B3 边界 off-by-one）
```diff
-    if not m and p <= 0:
+    if not m and p < 0:
```
**变异语义**：边界从 `<= 0` 退回 `< 0`，`p == 0` 的零值不再命中快捷分支 → 走量化 → prec=0 → ValueError。直接还原原 bug 的 off-by-one。保留。

### Group B — 替换（C1 值：不同字面量排除 0）
**原**：与 A 字节相同（`p <= 0`→`p < 0`）。
**最终 mutation**：
```diff
-    if not m and p <= 0:
+    if not m and p <= -1:
```
**变异语义**：边界改成 `p <= -1`，效果上同样排除 `p == 0`（`0 <= -1` 为假），但用了不同的字面量。比 A 的 `< 0` 多了一层迷惑性——`<= -1` 看起来是"刻意只处理负精度"，实则等价于排除 0。零值 p=0 仍走量化崩溃。模拟"用错误的边界字面量"。

### Group C — 替换（B3 条件：真值测试排除 0）
**原**：与 A 字节相同。
**最终 mutation**：
```diff
-    if not m and p <= 0:
+    if not m and p:
```
**变异语义**：把 `p <= 0` 改成对 `p` 的真值测试。`p == 0` 时 `p` 为假 → 不进快捷分支（与意图相反——0 正是要处理的）；非零 p（含负数）才进。零值 p=0 走量化崩溃。模拟"把比较误写成真值判断"——`if not m and p:` 看起来像"有精度时才特殊处理"，实则把 0 这个关键边界排除了。

### Group D — 替换（C1 条件变量：require m truthy）
**原**：与 A 字节相同。
**最终 mutation**：
```diff
-    if not m and p <= 0:
+    if m and p <= 0:
```
**变异语义**：把 `not m`（值无小数部分）改成 `m`（值有小数部分）。零值的 `m == 0`（falsy）→ `m and ...` 为假 → 不进快捷分支 → 量化崩溃。逻辑反转了对 m 的要求——快捷整数格式化本应针对"无小数部分"的值，改成只对"有小数部分"的值生效，零值反而被排除。模拟"`not m`/`m` 条件写反"。

### Group E — 保留（E2 隐式→显式开关）
```diff
-def floatformat(text, arg=-1):
+def floatformat(text, arg=-1, strict_precision=True):
...
-    if not m and p <= 0:
+    if not m and p <= 0 and not strict_precision:
```
**变异语义**：新增参数 `strict_precision`（默认 True），快捷分支只在 `not strict_precision` 时进入。默认 True → 永不进快捷分支 → 零值 p=0 走量化崩溃。只有显式传 `strict_precision=False` 才修复。模拟"把零值快捷处理做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

原 A、B、C、D 字节完全相同（`p <= 0`→`p < 0`）。本次保留 A（off-by-one `< 0`）、E（默认关闭开关），重做 B（`p <= -1` 不同字面量排除 0）、C（`if not m and p` 真值测试排除 0）、D（`if m and p <= 0` 反转 m 条件排除零值）。本 patch 仅一行条件判断，五组围绕"如何把 p=0 零值排除出快捷分支"分化为"off-by-one / 错误字面量 / 真值测试 / 反转 m 条件 / 默认关闭开关"五个角度。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P（`test_zero_values`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
