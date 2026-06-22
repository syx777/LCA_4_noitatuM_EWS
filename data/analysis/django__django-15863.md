# django__django-15863

## 问题背景

模板过滤器 `floatformat` 对高精度 `Decimal` 丢失精度。例如 `Decimal('42.12345678901234567890')|floatformat:20` 输出 `42.12345678901234400000`（被截断到 float 精度）。根因：实现把输入用 `repr(text)` 转字符串再构造 `Decimal`，而 `repr(Decimal)` 形如 `Decimal('42.12...')`（带引号和类名），`Decimal(repr(text))` 抛 `InvalidOperation`，落入 `except` 分支用 `Decimal(str(float(text)))`——经过 float 中转，精度被砍。Golden patch 把 `input_val = repr(text)` 改成 `input_val = str(text)`：`str(Decimal)` 返回纯数字串 `'42.12345...'`，`Decimal(str(text))` 直接精确构造，不再触发 float 回退。

## Golden Patch 语义分析

```python
try:
    input_val = str(text)        # 原为 repr(text)
    d = Decimal(input_val)
except InvalidOperation:
    try:
        d = Decimal(str(float(text)))
    except (ValueError, InvalidOperation, TypeError):
        return ""
```
核心语义：**用 `str(text)` 而非 `repr(text)` 构造 Decimal**。`str(Decimal('42.1'))` == `'42.1'`（可直接喂给 `Decimal`），而 `repr` 是 `"Decimal('42.1')"`（无法解析）。改对之后 Decimal 输入走 `Decimal(str(text))` 精确路径，绕开 `except` 里的 `float()` 精度损失。float 中转只应作为非数字字符串的兜底，不应吞掉 Decimal。

F2P 测试 `FunctionTests.test_inputs` 新增 `floatformat(Decimal("123456.123456789012345678901"), 21) == "123456.123456789012345678901"`，断言 21 位小数完整保留。

## 调用链分析

`floatformat(text, arg)` → 解析 arg 的后缀标志 → `try: input_val=str(text); d=Decimal(input_val)`；失败回退 `Decimal(str(float(text)))`。随后用 `d.quantize(exp, ROUND_HALF_UP, Context(prec=prec))` 按 `prec`（基于位数动态算）舍入，再从 `as_tuple()` 拼出最终字符串。精度由两处共同保证：(1) Decimal 构造不经 float；(2) `prec` 足够大不截断有效位。任一被破坏都丢精度。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟡 语义浅层 | 保留 | 默认参数 `arg=-1`→`arg=0`，改变默认小数位 |
| B | 🟢 高质量 | 保留 | 末尾补零循环 `<=`→`<`，少补一位零 |
| C | 🟢 高质量 | 保留 | `str(text)`→`repr(text)`，直接还原原 bug |
| D | 🔴 必须替换 | 替换 | 原 D 含 `# BUG:` 露骨注释且与 E 重复；改为 `prec` 上限截断 |
| E | 🔴 必须替换 | 替换 | 原 E 与 D 功能完全相同（isinstance Decimal→float）；保留为自然的 isinstance 分支 |

原 D、E 两组逻辑完全相同（都用 `isinstance(text, Decimal)` 转 float），且 D 带 `# BUG:` 露骨注释。重做 D 为另一种精度损失机制（`prec` 截断），E 保留 isinstance 形态但去掉露骨注释。

## 各组 Mutation 分析

### Group A — 保留（B-默认参数边界）
```diff
-def floatformat(text, arg=-1):
+def floatformat(text, arg=0):
```
**变异语义**：默认小数位从 `-1`（最多 1 位、整数则不显示小数）改成 `0`（始终 0 位、四舍五入到整数）。不传 arg 的调用结果改变（如 `floatformat(13.1) ` 由 `13.1` 变 `13`）。F2P 用例显式传 arg，但 `test_inputs` 内有多处不传/传 -1 的断言会失败。位置在函数签名、影响所有默认调用，属关键边界。保留。

### Group B — 保留（B3 边界：补零循环 off-by-one）
```diff
-    while len(digits) <= abs(exponent):
+    while len(digits) < abs(exponent):
```
**变异语义**：构造结果字符串时补零的循环条件由 `<=` 改 `<`，少补一个零。`digits.insert(-exponent, ".")` 在 digits 长度不足时插入位置错误，导致小数点位置错乱或缺前导零。典型小数能蒙混，特定指数/位数组合下输出错误。保留。

### Group C — 保留（C1 类型/数据形状：repr 还原 bug）
```diff
-        input_val = str(text)
+        input_val = repr(text)
```
**变异语义**：把 `str(text)` 改回 `repr(text)`，直接还原原始 bug——`Decimal(repr(Decimal(...)))` 抛 InvalidOperation，落入 float 回退，高精度 Decimal 丢精度。保留。

### Group D — 替换（B2/C 精度上限截断）
**原 mutation**：`if isinstance(text, Decimal): d = Decimal(str(float(text)))` 且带 `# BUG:` 注释（与 E 重复、注释露骨）。
**最终 mutation**：
```diff
-    prec = abs(p) + units + 1
+    prec = min(abs(p) + units + 1, 16)
```
**变异语义**：把动态计算的 `prec`（量化精度）截断到上限 16（接近 double 的有效十进制位数）。`d.quantize(exp, ROUND_HALF_UP, Context(prec=...))` 在 prec 被压到 16 时对超过 16 位有效数字的 Decimal 舍入，丢失高位精度——表现与原 float bug 几乎一样（约 15–16 位后失真），但根因藏在"精度上限"而非输入转换，极隐蔽。普通小数（位数 ≤16）完全不受影响。比原 D 的露骨注释自然得多。

### Group E — 替换（D1 状态：自然的 isinstance 回退）
**原 mutation**：与 D 完全相同（isinstance Decimal→float），带 `# Convert Decimal to float for compatibility` 注释。
**最终 mutation**：
```diff
-        input_val = str(text)
-        d = Decimal(input_val)
+        input_val = str(text)
+        # Convert Decimal to float for compatibility
+        if isinstance(text, Decimal):
+            d = Decimal(str(float(text)))
+        else:
+            d = Decimal(input_val)
```
**变异语义**：对 `Decimal` 输入显式走 `Decimal(str(float(text)))`——经 float 中转丢精度；非 Decimal 走 `Decimal(str(text))` 精确路径。注释写"为兼容性转 float"看起来是合理的兼容处理，实则恰好对最需要精度的 Decimal 丢精度。与 D 现在机制不同（D 在 quantize 阶段截断 prec，E 在输入构造阶段经 float），二者正交。保留为 E（去掉了原 D 的 `# BUG:` 露骨字样，改用看似正当的兼容性说辞）。

## 新设计 Mutation 说明

原 D、E 两组功能完全相同（都用 `isinstance(text, Decimal)` 转 float 丢精度），且 D 带 `# BUG:` 露骨注释易被审查发现。本次保留 A（默认参数）、B（补零 off-by-one）、C（repr 还原），把 D 重做为 quantize 阶段的 `prec` 上限截断（精度损失藏在精度计算而非输入转换），E 保留 isinstance 输入回退但去掉露骨注释、改用看似正当的"兼容性"说辞。五组覆盖"默认参数 / 补零边界 / repr 输入 / prec 截断 / float 输入回退"五个角度，且 D 与 E 机制正交（量化阶段 vs 构造阶段）。全部实测：golden 通过、五个变异均令 F2P（`test_inputs`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
