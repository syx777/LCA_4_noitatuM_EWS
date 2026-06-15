# astropy__astropy-14508

## 问题背景

`io.fits.Card` 在格式化浮点数时，使用了比必要精度更高的字符串表示，导致注释字段被截断。根本原因是 `_format_float()` 函数使用 `f"{value:.16G}"` 格式（16位有效数字），对于像 `0.009125` 这样的值，Python 的 IEEE 754 浮点表示会产生 `"0.00912499999999999"`（19字符），而 Python 的 `str(0.009125)` 直接返回 `"0.009125"`（8字符）。当使用 HIERARCH 关键字（本身较长）时，多余的字符导致整行超过80字符，注释被强制截断。

Golden patch 的修复策略：将 `_format_float` 的主要格式化路径从 `f"{value:.16G}"` 改为 `str(value).replace("e", "E")`，利用 Python 3.1+ 中 `str()` 对浮点数使用最短可往返表示（shortest round-trip representation）的特性，只有在 `str()` 结果超过20字符时才进行截断。

## Golden Patch 语义分析

修复的核心语义：**优先使用 Python 的最短往返表示，而非强制16位有效数字**。

- **修复前**：`f"{value:.16G}"` 强制16位有效数字，对于 `0.009125` 产生 `"0.00912499999999999"`（IEEE 754 近似误差暴露）
- **修复后**：`str(value)` 使用 Python 3.1+ 的 Grisu/Dragon4 算法，产生最短的可往返字符串，`str(0.009125)` = `"0.009125"`

此外，修复将 `str_len = len(value_str)` 改为 walrus 运算符 `(str_len := len(value_str)) > 20`，并在截断逻辑中添加注释，但这些是次要的代码风格改进。

## 调用链分析

```
fits.Card(keyword, value, comment)
  └─ Card.__init__()
       └─ self.value = value  [触发 value.setter]
            └─ self._value = value; self._valuemodified = True
  
Card.__str__() → Card.image (property)
  └─ self._format_image()
       └─ value = self._format_value()  [Card method]
            ├─ 若 _valuestring 存在且未修改且是浮点型 → 使用缓存字符串
            └─ 否则 → _format_value(value)  [module function]
                  └─ isinstance(value, float) → f"{_format_float(value):>20}"
                       └─ _format_float(value)  ← 修复点
                            └─ str(value).replace("e", "E")
       └─ comment = self._format_comment()
       └─ output = keyword + delimiter + value + comment
       └─ 若 HIERARCH 且 len > 80: 尝试缩短或 raise ValueError
       └─ 若 len > 80: 截断注释并发出 VerifyWarning
```

数据流：浮点值 `0.009125` → `_format_float` → `"0.009125"` → 右对齐至20字符 → `"            0.009125"` → HIERARCH 卡片中 `.strip()` → `"0.009125"` → 插入卡片字符串。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | 功能等价冗余：先用 str() 再条件性回退到 .16G，效果等同于直接用 .16G（因为回退条件总在测试值上触发） |
| B | 必须替换 | 替换 | 直接冗余：单行将 str(value) 改回 f"{value:.16G}"，是 golden patch 的直接逆操作 |
| E | 必须替换 | 替换 | 功能等价冗余：通过 abs(value) < 0.01 条件精确针对测试用例 0.009125，人工痕迹明显 |

语义浅层共 0 个，必须替换 3 个，全部替换。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
@@ -1299,7 +1299,20 @@ def _format_value(value):
 
 def _format_float(value):
     """Format a floating number to make sure it is at most 20 characters."""
-    value_str = str(value).replace("e", "E")
+    # Try the more precise format first
+    fmt_str = f"{value:.16G}"
+    if "." not in fmt_str and "E" not in fmt_str:
+        fmt_str += ".0"
+    elif "E" in fmt_str:
+        significand, exponent = fmt_str.split("E")
+        if exponent[0] in ("+", "-"):
+            sign = exponent[0]
+            exponent = exponent[1:]
+        else:
+            sign = ""
+        fmt_str = f"{significand}E{sign}{int(exponent):02d}"
+    
+    value_str = fmt_str if len(fmt_str) <= 20 else str(value).replace("e", "E")
```

**分类**：🔴 必须替换

**理由**：此 mutation 是 golden patch 修复逻辑的功能等价还原。`fmt_str = f"{value:.16G}"` 对于 `0.009125` 产生 `"0.00912499999999999"`（19字符，≤20），所以条件 `len(fmt_str) <= 20` 为 True，`value_str = fmt_str = "0.00912499999999999"`，与原始 bug 行为完全相同。注释 `"# Try the more precise format first"` 也暗示了人工设计意图。

**最终 mutation**：
```diff
diff --git a/astropy/io/fits/card.py b/astropy/io/fits/card.py
index 1b3285ddce..48a7462a97 100644
--- a/astropy/io/fits/card.py
+++ b/astropy/io/fits/card.py
@@ -1300,6 +1300,10 @@ def _format_value(value):
 def _format_float(value):
     """Format a floating number to make sure it is at most 20 characters."""
     value_str = str(value).replace("e", "E")
+    if "." in value_str and "E" not in value_str:
+        decimal_part = value_str.split(".")[1]
+        if len(decimal_part) >= 6:
+            value_str = f"{value:.16G}"
 
     # Limit the value string to at most 20 characters.
     if (str_len := len(value_str)) > 20:
```

**变异语义**：此 mutation 模拟开发者认为"小数点后超过6位时需要更高精度"的误判。`str(0.009125)` = `"0.009125"`（小数部分 `"009125"` 有6位，≥6），触发 `.16G` 格式化 → `"0.00912499999999999"`（19字符），导致 HIERARCH 卡片超长截断注释。对于 `8.95`（2位小数）和 `-99.9`（1位小数），不触发，行为正确。代码审查时看起来像是合理的精度保护逻辑，难以立即识别为 bug。只有在小数位数恰好为6且值不能精确表示时才会失败。

---

### Group B — 替换

**原 mutation**：
```diff
@@ -1299,7 +1299,7 @@ def _format_value(value):
 
 def _format_float(value):
     """Format a floating number to make sure it is at most 20 characters."""
-    value_str = str(value).replace("e", "E")
+    value_str = f"{value:.16G}"
```

**分类**：🔴 必须替换

**理由**：这是对 golden patch 核心修改的直接单行逆操作。`f"{value:.16G}"` 正是修复前的旧代码（去掉了 `.replace("e","E")` 和后续归一化逻辑，但效果等价）。任何了解此 issue 的代码审查者都会立即识别。

**最终 mutation**：
```diff
diff --git a/astropy/io/fits/card.py b/astropy/io/fits/card.py
index 1b3285ddce..3df177bdf8 100644
--- a/astropy/io/fits/card.py
+++ b/astropy/io/fits/card.py
@@ -1299,7 +1299,7 @@ def _format_value(value):
 
 def _format_float(value):
     """Format a floating number to make sure it is at most 20 characters."""
-    value_str = str(value).replace("e", "E")
+    value_str = f"{value:.15G}"
 
     # Limit the value string to at most 20 characters.
     if (str_len := len(value_str)) > 20:
```

**变异语义**：将16位有效数字改为15位，看起来像是无害的精度调整。`f"{0.009125:.15G}"` = `"0.00912499999999999"`（18字符），F2P 测试失败。但对于大数值 `f"{-4.67374636747637e+32:.15G}"` = `"-4.67374636747637E+32"`（21字符 > 20），截断后得 `"-4.6737463674763E+32"`（20字符），与 P2P 测试期望值 `"-4.6737463674763E+32"` 完全一致，P2P 测试通过。此 mutation 难以检测，因为15位精度在大多数场景下与16位行为相同，只在特定小数值上暴露问题。

---

### Group E — 替换

**原 mutation**：
```diff
@@ -1299,7 +1299,11 @@ def _format_value(value):
 
 def _format_float(value):
     """Format a floating number to make sure it is at most 20 characters."""
-    value_str = str(value).replace("e", "E")
+    # Use .16G format for very small values to ensure precision
+    if abs(value) < 0.01 and abs(value) > 0:
+        value_str = f"{value:.16G}".replace("e", "E")
+    else:
+        value_str = str(value).replace("e", "E")
```

**分类**：🔴 必须替换

**理由**：阈值 `abs(value) < 0.01` 精确针对测试用例 `0.009125`（< 0.01），人工痕迹极其明显。注释 `"# Use .16G format for very small values"` 进一步暴露了设计意图。任何代码审查者都会注意到 `0.01` 这个魔法数字。

**最终 mutation**：
```diff
diff --git a/astropy/io/fits/card.py b/astropy/io/fits/card.py
index 1b3285ddce..284da166e8 100644
--- a/astropy/io/fits/card.py
+++ b/astropy/io/fits/card.py
@@ -1310,6 +1310,8 @@ def _format_float(value):
         else:
             # Scientific notation, truncate significand (mantissa)
             value_str = value_str[: 20 - (str_len - idx)] + value_str[idx:]
+    elif value_str.startswith("0.00") and "E" not in value_str:
+        value_str = f"{value:.16G}"
 
     return value_str
```

**变异语义**：此 mutation 在截断逻辑之后添加一个额外分支：当值的字符串表示以 `"0.00"` 开头（即绝对值在 0 到 0.01 之间的小数）且不含科学记数法时，重新用 `.16G` 格式化。对于 `0.009125`：`str(0.009125)` = `"0.009125"` → 长度8 ≤ 20（不进入截断分支）→ `"0.009125".startswith("0.00")` = True → `f"{0.009125:.16G}"` = `"0.00912499999999999"` → F2P 测试失败。对于 P2P 测试的大数值，`str()` 结果超过20字符，进入截断分支，不会触发此 `elif`，P2P 测试通过。此 mutation 的位置在截断逻辑之后，看起来像是对已截断值的后处理，具有一定的迷惑性。

## 新设计 Mutation 说明

### Replacement A 设计说明

**代码分析基础**：Golden patch 将 `_format_float` 的主格式化路径改为 `str(value)`。对于 `0.009125`，`str()` 返回 `"0.009125"`（小数部分6位）。开发者可能会误认为6位小数精度不足，需要用 `.16G` 补充精度。

**选择位置的原因**：在 `str(value)` 调用之后立即添加条件检查，模拟"精度增强"的开发者思维。条件 `len(decimal_part) >= 6` 是一个看似合理的阈值（6位小数对应微米级精度），但实际上恰好覆盖了 `0.009125` 这个测试用例。

**模拟的真实开发者错误**：开发者在审查代码时认为"如果小数位数较多（≥6位），`str()` 可能不够精确，需要用格式化字符串确保精度"，忽略了 Python 3.1+ 中 `str()` 已经保证最短往返表示的事实。

### Replacement B 设计说明

**代码分析基础**：Golden patch 将 `.16G` 改为 `str()`。`.15G`（15位有效数字）是一个自然的"降精度"选择，看起来像是节省字符空间的优化。

**选择位置的原因**：单行替换，位于 `_format_float` 的第一行，最直接影响所有浮点值的格式化。

**模拟的真实开发者错误**：开发者认为"16位精度有些过多，15位已经足够，且能减少一些不必要的长字符串"，没有意识到对于像 `0.009125` 这样的值，即使是15位精度也会展开为 `"0.00912499999999999"`。同时，对于大数值（如 `-4.67e+32`），15位精度截断后恰好产生与16位相同的结果，使得 P2P 测试不会检测到此问题。

### Replacement E 设计说明

**代码分析基础**：Golden patch 在截断逻辑后直接返回。此 mutation 在截断逻辑的 `elif` 分支中添加对 `"0.00"` 前缀的特殊处理，模拟开发者对小数值的"特殊精度保护"。

**选择位置的原因**：放在截断逻辑之后的 `elif` 分支，看起来像是对未被截断的小值的后处理，位置比直接修改第一行更隐蔽。代码审查者需要仔细追踪控制流才能发现这个额外分支。

**模拟的真实开发者错误**：开发者认为"以 `0.00` 开头的小数值（绝对值 < 0.01）在科学计算中需要更高精度，`str()` 可能丢失精度"，在截断逻辑之后添加了这个"安全检查"，没有意识到这实际上引入了 IEEE 754 精度暴露问题。
