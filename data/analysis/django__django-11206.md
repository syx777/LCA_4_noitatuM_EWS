# django__django-11206

## 问题背景

`utils.numberformat.format` 函数在处理极小 Decimal 数时会以科学计数法输出，即使已指定 `decimal_pos` 参数。例如：
- `nformat(Decimal('1e-199'), '.', decimal_pos=2)` → `'0.00'`（正确）
- `nformat(Decimal('1e-200'), '.', decimal_pos=2)` → `'1.00e-200'`（错误，应显示 `'0.00'`）

根本原因：代码中有一个硬编码的 200 位数字阈值。当 Decimal 的绝对指数 + 有效位数超过 200 时，强制走科学计数法路径（避免 `{:f}` 格式化消耗大量内存）。但当 `decimal_pos` 指定时，这类极小数字实际上应该被显示为全零（`0.000`），不应输出科学计数法。

## Golden Patch 语义分析

Golden patch 在 `if isinstance(number, Decimal):` 块的最开头（200位数字检查之前）插入：

```python
if decimal_pos is not None:
    # If the provided number is too small to affect any of the visible
    # decimal places, consider it equal to '0'.
    cutoff = Decimal('0.' + '1'.rjust(decimal_pos, '0'))
    if abs(number) < cutoff:
        number = Decimal('0')
```

**核心语义**：
1. 计算截断阈值 `cutoff`：对于 `decimal_pos=3`，`cutoff = Decimal('0.001')`。
2. 如果 `abs(number) < cutoff`，则该数字在 `decimal_pos` 位小数内无任何可见贡献，直接归零。
3. 将 `number` 设为 `Decimal('0')` 后，后续 200 位检查不会被触发（0 的指数为 0），走 `{:f}` 路径，正确输出 `'0.000'`。

**关键顺序**：截断检查必须在 200 位检查之前执行。如果顺序颠倒，极小数字（如 `1.234e-303`，其 abs(exponent)+len(digits) > 200）会先进入科学计数法路径并直接返回，永远不会被归零。

## 调用链分析

`format()` 是独立函数，无继承关系，主要被以下场景调用：
- `django/utils/formats.py` → `number_format()`（提供 l10n 格式化）
- `django/contrib/humanize/templatetags/humanize.py` → `intcomma()`、`intword()` 等
- 递归调用自身：处理科学计数法数字时，将 coefficient 字符串递归传入 `format()`

被调用的内部函数：`Decimal.as_tuple()`、`Decimal.quantize()`（mutation C 中使用）

数据流：`number`（Decimal/int/float/str）→ 符号提取 → 整数部分/小数部分分割 → 分组处理 → 拼接输出。

## 替换决策总览

**原始 mutations.jsonl 中仅有 B、C、E 三组（缺 A、D），且全部质量不合格：**

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 缺失 | 新设计 | mutations.jsonl 中无此组 |
| B | 🔴 必须替换 | 替换 | `<` → `<=` 会破坏 P2P 测试（`1e-8 == cutoff`），不破坏 F2P 测试 |
| C | 🔴 必须替换 | 替换 | `Decimal(10**-dp)` 浮点精度误差破坏 P2P（`1e-8` 被误判），不破坏 F2P |
| D | 缺失 | 新设计 | mutations.jsonl 中无此组 |
| E | 🔴 必须替换 | 替换 | 添加 `round_small_decimals=False` 参数不自然，且完全禁用修复使所有情况失败 |

**关键诊断**：所有原始 mutation 均无法正确地"只破坏 F2P 测试而不破坏 P2P 测试"，或反向破坏。

F2P 测试：`nformat(Decimal('0.{0*299}1234'), '.', decimal_pos=3) == '0.000'`  
该数字的特征：`abs(exponent) + len(digits) = 303 + 4 = 307 > 200`，会触发科学计数法路径。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：（原 mutations.jsonl 中缺失此组）

**分类**：🔴 必须替换（缺失）

**理由**：需新设计 A1（参数语义修改）类型的 mutation。

**最终 mutation**：
```diff
diff --git a/django/utils/numberformat.py b/django/utils/numberformat.py
index 961a60e37d..815b839a4c 100644
--- a/django/utils/numberformat.py
+++ b/django/utils/numberformat.py
@@ -27,17 +27,16 @@ def format(number, decimal_sep, decimal_pos=None, grouping=0, thousand_sep='',
     # sign
     sign = ''
     if isinstance(number, Decimal):
-
-        if decimal_pos is not None:
+        # Format values with more than 200 digits (an arbitrary cutoff) using
+        # scientific notation to avoid high memory usage in {:f}'.format().
+        _, digits, exponent = number.as_tuple()
+        if decimal_pos is not None and not (abs(exponent) + len(digits) > 200):
             # If the provided number is too small to affect any of the visible
             # decimal places, consider it equal to '0'.
             cutoff = Decimal('0.' + '1'.rjust(decimal_pos, '0'))
             if abs(number) < cutoff:
                 number = Decimal('0')
-
-        # Format values with more than 200 digits (an arbitrary cutoff) using
-        # scientific notation to avoid high memory usage in {:f}'.format().
-        _, digits, exponent = number.as_tuple()
+            _, digits, exponent = number.as_tuple()
         if abs(exponent) + len(digits) > 200:
             number = '{:e}'.format(number)
             coefficient, exponent = number.split('e')
```

**变异语义**：将 `as_tuple()` 的计算提前，并在截断检查中添加 `not (abs(exponent) + len(digits) > 200)` 条件。这使得极小但指数位数 > 200 的数字（如 F2P 测试值）绕过截断检查，直接走科学计数法路径输出 `1.234e-300`。

**难以发现原因**：代码看起来是一次合理的"重构"——将 `as_tuple()` 提前以避免重复计算，同时添加了"优化条件"。逻辑上似乎说得通：对于已经需要科学计数法的数字，不需要额外的截断检查。错误微妙在于：截断检查将 number 改为 0 后，原本 > 200 位的数字就不再需要科学计数法了。

**F2P 失败**：`0.{0*299}1234`（307 > 200）→ 条件 `not (307 > 200)` 为 False → 截断跳过 → 科学计数法路径 → `'1.234e-300'` ≠ `'0.000'`。

**P2P 通过**：所有 ≤200 位的数字（`9e-19`、`1e-9` 等），条件 `not (≤200 > 200)` 为 True → 截断正常应用，效果与 golden 相同。

---

### Group B — 替换

**原 mutation**（`<` → `<=`）：
```diff
-            if abs(number) < cutoff:
+            if abs(number) <= cutoff:
```

**分类**：🔴 必须替换

**理由**：`Decimal('1e-8')` 恰好等于 `cutoff`（decimal_pos=8 时），`<=` 使其被归零，输出 `'0.00000000'` 而非 `'0.00000001'`，破坏 P2P 测试。同时 F2P 测试不受影响（`1.234e-303 ≤ 0.001` 也是 True）。

**最终 mutation**：
```diff
diff --git a/django/utils/numberformat.py b/django/utils/numberformat.py
index 961a60e37d..5e19105b6b 100644
--- a/django/utils/numberformat.py
+++ b/django/utils/numberformat.py
@@ -27,14 +27,6 @@ def format(number, decimal_sep, decimal_pos=None, grouping=0, thousand_sep='',
     # sign
     sign = ''
     if isinstance(number, Decimal):
-
-        if decimal_pos is not None:
-            # If the provided number is too small to affect any of the visible
-            # decimal places, consider it equal to '0'.
-            cutoff = Decimal('0.' + '1'.rjust(decimal_pos, '0'))
-            if abs(number) < cutoff:
-                number = Decimal('0')
-
         # Format values with more than 200 digits (an arbitrary cutoff) using
         # scientific notation to avoid high memory usage in {:f}'.format().
         _, digits, exponent = number.as_tuple()
@@ -48,6 +40,12 @@ def format(number, decimal_sep, decimal_pos=None, grouping=0, thousand_sep='',
             )
             return '{}e{}'.format(coefficient, exponent)
         else:
+            if decimal_pos is not None:
+                # If the provided number is too small to affect any of the visible
+                # decimal places, consider it equal to '0'.
+                cutoff = Decimal('0.' + '1'.rjust(decimal_pos, '0'))
+                if abs(number) < cutoff:
+                    number = Decimal('0')
             str_number = '{:f}'.format(number)
     else:
         str_number = str(number)
```

**变异语义**：将截断检查块整体移入 `else` 分支（≤200 位数字路径）。开发者误认为截断只需要在使用 `{:f}` 格式化时处理。F2P 测试数字（307 > 200）会直接进入 `if abs(exponent) + len(digits) > 200:` 分支，在截断执行之前就被转为科学计数法。

**F2P 失败**：`0.{0*299}1234` 的 307 > 200 → 直接进入科学计数法路径 → `'1.234e-300'`。

**P2P 通过**：所有 P2P 测试中的小数字（≤200 位），进入 `else` 分支 → 截断正常应用 → 与 golden 效果相同。

---

### Group C — 替换

**原 mutation**（`Decimal('0.' + '1'.rjust(...))` → `Decimal(10 ** (-decimal_pos))`）：
```diff
-            cutoff = Decimal('0.' + '1'.rjust(decimal_pos, '0'))
+            cutoff = Decimal(10 ** (-decimal_pos))
```

**分类**：🔴 必须替换

**理由**：`Decimal(10**-8)` 比 `Decimal('1e-8')` 略大（浮点精度误差），导致 `Decimal('1e-8') < Decimal(10**-8)` 为 True，将 P2P 测试 `('1e-8', 8, '0.00000001')` 错误归零为 `'0.00000000'`。同时 F2P 测试不受影响（`1e-303 < Decimal(10**-3)` 仍为 True）。

**最终 mutation**：
```diff
diff --git a/django/utils/numberformat.py b/django/utils/numberformat.py
index 961a60e37d..bfe8dac91a 100644
--- a/django/utils/numberformat.py
+++ b/django/utils/numberformat.py
@@ -27,14 +27,6 @@ def format(number, decimal_sep, decimal_pos=None, grouping=0, thousand_sep='',
     # sign
     sign = ''
     if isinstance(number, Decimal):
-
-        if decimal_pos is not None:
-            # If the provided number is too small to affect any of the visible
-            # decimal places, consider it equal to '0'.
-            cutoff = Decimal('0.' + '1'.rjust(decimal_pos, '0'))
-            if abs(number) < cutoff:
-                number = Decimal('0')
-
         # Format values with more than 200 digits (an arbitrary cutoff) using
         # scientific notation to avoid high memory usage in {:f}'.format().
         _, digits, exponent = number.as_tuple()
@@ -48,6 +40,12 @@ def format(number, decimal_sep, decimal_pos=None, grouping=0, thousand_sep='',
             )
             return '{}e{}'.format(coefficient, exponent)
         else:
+            if decimal_pos is not None:
+                # If the provided number is too small to affect any of the visible
+                # decimal places, consider it equal to '0'.
+                cutoff = Decimal('0.' + '1'.rjust(decimal_pos, '0'))
+                if abs(number) < cutoff:
+                    number = number.quantize(cutoff) * 0
             str_number = '{:f}'.format(number)
     else:
         str_number = str(number)
```

**变异语义**：将截断块移入 `else` 分支，同时将 `number = Decimal('0')` 改为 `number = number.quantize(cutoff) * 0`。`quantize(cutoff)` 将数字精度对齐到 cutoff 精度，再乘以 0 得到 0。表面上看这是"更精确地"将数字归零（保留精度信息），实际上功能完全等同于 `Decimal('0')`，但关键是截断块被移到了 `else` 分支中，F2P 数字绕过了截断。

**难以发现原因**：`number.quantize(cutoff) * 0` 看起来是一种"类型感知"的归零方式，对代码审查者来说似乎更严谨。

**F2P 失败**：同 Group B，307 > 200 → else 分支不执行 → 科学计数法。

---

### Group D — 替换

**原 mutation**：（原 mutations.jsonl 中缺失此组）

**分类**：🔴 必须替换（缺失）

**最终 mutation**：
```diff
diff --git a/django/utils/numberformat.py b/django/utils/numberformat.py
index 961a60e37d..c9dae68660 100644
--- a/django/utils/numberformat.py
+++ b/django/utils/numberformat.py
@@ -27,14 +27,6 @@ def format(number, decimal_sep, decimal_pos=None, grouping=0, thousand_sep='',
     # sign
     sign = ''
     if isinstance(number, Decimal):
-
-        if decimal_pos is not None:
-            # If the provided number is too small to affect any of the visible
-            # decimal places, consider it equal to '0'.
-            cutoff = Decimal('0.' + '1'.rjust(decimal_pos, '0'))
-            if abs(number) < cutoff:
-                number = Decimal('0')
-
         # Format values with more than 200 digits (an arbitrary cutoff) using
         # scientific notation to avoid high memory usage in {:f}'.format().
         _, digits, exponent = number.as_tuple()
@@ -48,6 +40,12 @@ def format(number, decimal_sep, decimal_pos=None, grouping=0, thousand_sep='',
             )
             return '{}e{}'.format(coefficient, exponent)
         else:
+            if decimal_pos is not None:
+                # If the provided number is too small to affect any of the visible
+                # decimal places, consider it equal to '0'.
+                cutoff = Decimal('0.' + '1'.rjust(decimal_pos + 1, '0'))
+                if abs(number) < cutoff:
+                    number = Decimal('0')
             str_number = '{:f}'.format(number)
     else:
         str_number = str(number)
```

**变异语义**：将截断块移入 `else` 分支，同时将 `decimal_pos` 改为 `decimal_pos + 1` 计算截断阈值。这使截断精度比预期少一位：`decimal_pos=3` 时 cutoff 为 `0.0001` 而非 `0.001`。

**难以发现原因**：`decimal_pos + 1` 可以被解释为"截断到比显示位数更精确一位的精度"（一种常见的数值处理技巧，避免显示边界值的舍入歧义）。结合位置移动，双重微小错误使 bug 更难被发现。

**F2P 失败**：同 Group B/C，移入 else 分支后，F2P 数字（307 > 200）绕过截断。

**P2P 通过**：对于所有 P2P 测试中的极小数字，`decimal_pos + 1` 的截断阈值（10倍更严格）仍然能将它们归零，输出与 golden 相同：
- `9e-19` < `Decimal('0.001')` (dp+1=3时cutoff=0.001 for dp=2) → `'0.00'` ✓
- `1e-9` < cutoff_dp9=`1e-9`? No → `{:f}` → `dec_part[:8]='00000000'` → `'0.00000000'` ✓

---

### Group E — 替换

**原 mutation**（添加 `round_small_decimals=False` 参数）：
```diff
-def format(number, decimal_sep, decimal_pos=None, grouping=0, thousand_sep='',
-           force_grouping=False, use_l10n=None):
+def format(number, decimal_sep, decimal_pos=None, grouping=0, thousand_sep='',
+           force_grouping=False, use_l10n=None, round_small_decimals=False):
...
-        if decimal_pos is not None:
+        if decimal_pos is not None and round_small_decimals:
```

**分类**：🔴 必须替换

**理由**：此 mutation 将修复变为可选参数（默认关闭），完全禁用了修复，导致所有 F2P 和相关场景都失败。`round_small_decimals` 参数名不自然，在实际代码中不会出现这样的参数设计。

**最终 mutation**：
```diff
diff --git a/django/utils/numberformat.py b/django/utils/numberformat.py
index 961a60e37d..8cc1339c28 100644
--- a/django/utils/numberformat.py
+++ b/django/utils/numberformat.py
@@ -28,7 +28,7 @@ def format(number, decimal_sep, decimal_pos=None, grouping=0, thousand_sep='',
     sign = ''
     if isinstance(number, Decimal):
 
-        if decimal_pos is not None:
+        if decimal_pos is not None and number.is_signed():
             # If the provided number is too small to affect any of the visible
             # decimal places, consider it equal to '0'.
             cutoff = Decimal('0.' + '1'.rjust(decimal_pos, '0'))
```

**变异语义**：在截断检查中添加 `and number.is_signed()` 条件，使截断只对负数生效。`is_signed()` 对负数和负零返回 True。F2P 测试使用正数 → `is_signed()=False` → 截断跳过 → 极小正 Decimal 进入科学计数法路径。

**难以发现原因**：`number.is_signed()` 是 Decimal 的正规 API，添加符号检查看起来像是一种"防御性编程"——确保只对有符号数字进行特殊处理。错误微妙在于绝大多数需要截断的小数字都是正数。

**F2P 失败**：正数 `0.{0*299}1234`，`is_signed()=False` → 截断跳过 → 307 > 200 → 科学计数法。

**P2P 通过**：
- `nformat(Decimal('9e-19'), '.', 2)`：正数 → 截断跳过，但 `{:f}` 给出 `0.0000000000000000009` → `dec_part[:2]='00'` → `'0.00'` ✓
- `nformat(Decimal('1e-9'), '.', 8)`：同理，`{:f}` → `dec_part[:8]='00000000'` → `'0.00000000'` ✓
- `nformat(Decimal('.00000000000099'), '.', 0)`：正数 → 截断跳过 → `{:f}` → `dec_part[:0]=''` → `'0'` ✓

## 新设计 Mutation 说明

### Group A 设计思路
基于代码分析：`as_tuple()` 被调用了两次（一次在截断检查后，一次在200位检查前）。开发者重构时可能会将 `as_tuple()` 提前到最顶部，然后在截断检查条件中加入"只对不需要科学计数法的数字应用截断"的约束，误以为这是等价变换。实际上，截断检查会将大指数数字归零，使其不再需要科学计数法，因此必须在200位检查之前执行。

### Group D 设计思路
基于代码分析：截断阈值的计算 `Decimal('0.' + '1'.rjust(decimal_pos, '0'))` 看起来像是将精度对齐到 `decimal_pos`。一个常见的数值处理直觉是"截断到比需要显示的精度多一位"（避免舍入边界），这会导致 `decimal_pos + 1` 的错误。结合移到 `else` 分支的位置错误，该 mutation 模拟了真实重构场景中可能犯的两个相互掩盖的错误。
