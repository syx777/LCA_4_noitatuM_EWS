# astropy__astropy-8707

## 问题背景

`Header.fromstring` 和 `Card.fromstring` 在 Python 3 中只接受 `str` 类型，不接受 `bytes` 类型。但用户通常以二进制模式读取 FITS 文件（`open(..., 'rb')`），得到 `bytes` 对象，传入这些方法时会报错。Golden patch 为两个方法都添加了 `bytes` 支持：在 `Card.fromstring` 中检测 `bytes` 并用 `latin1` 解码；在 `Header.fromstring` 中检测 `bytes` 并设置对应的 `bytes` 类型比较变量（`CONTINUE`、`END`、`end_card`、`sep`、`empty`）。

## Golden Patch 语义分析

**Card.fromstring（card.py）**：在 `card = cls()` 之后、`_pad(image)` 之前，添加 `if isinstance(image, bytes): image = image.decode('latin1')`。核心原因：`_pad` 函数中有 `input + ' ' * n`，`bytes + str` 在 Python 3 会 TypeError；且后续的关键字解析都依赖 `str` 类型。选择 `latin1` 而非 `ascii` 是因为 latin1 是字节透明编码（0x00-0xFF 一一对应），可以无损保留任何字节值，即使 FITS 头中有非 ASCII 字节也不会抛异常。

**Header.fromstring（header.py）**：在 `require_full_cardlength` 计算之后，添加 `if isinstance(data, bytes):` 分支，设置所有比较变量为 `bytes` 类型：
- `CONTINUE = b'CONTINUE'`：用于检测 CONTINUE 卡片（`next_image[:8] == CONTINUE`）
- `END = b'END'`：用于非全长卡片模式下检测 END（`next_image.split(sep)[0].rstrip() == END`）
- `end_card = END_CARD.encode('ascii')`：用于全长卡片模式下检测 END（`next_image == end_card`）
- `sep = sep.encode('latin1')`：将分隔符转为 bytes（默认 `b''`）
- `empty = b''`：用于 `empty.join(image)` 拼接 bytes 卡片块

所有比较变量必须与 `data` 的类型一致，否则 Python 3 的 bytes/str 比较永远返回 False。

## 调用链分析

```
用户调用 Header.fromstring(bytes_data)
  → isinstance(data, bytes) 检测
  → 循环切片 80 字节块 (next_image: bytes)
  → next_image[:8] == CONTINUE (bytes 比较)
  → next_image == end_card (bytes 比较，检测 END 卡片)
  → Card.fromstring(empty.join(image))  ← empty=b'', image=[bytes_chunk]
      → isinstance(image, bytes) 检测
      → image.decode('latin1')  ← 转为 str
      → _pad(image_str)  ← 填充到80字符
      → card._image = padded_str
  → Header._fromcards(cards)
```

`_pad(input)` 函数：计算 `len(input)` 并追加空格到80的倍数。若 input 为 bytes，`bytes + ' '*n` 会 TypeError。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 语义浅层 | 保留 | `latin1` → `ascii` 在两处关键编码位置，模拟真实的编码选择错误，边界情况下失败 |
| B | 语义浅层 | 替换 | `not isinstance` 翻转，过于直接，任何传入 bytes 的测试立即失败，是最弱的语义浅层 |
| C | 必须替换 | 替换 | 直接删除整个 bytes 分支，等同于还原 golden patch 的 header.py 部分 |
| D | 必须替换 | 替换 | 与 C 完全相同的 diff，重复冗余 |
| E | 必须替换 | 替换 | 显式 `raise TypeError` 含有明显人工痕迹，错误信息直接说明"不接受 bytes" |

语义浅层共 2 个（A、B），替换其中最弱的 floor(2/2) = 1 个：**B**。

## 各组 Mutation 分析

### Group A — 保留

**原 mutation**：
```diff
diff --git a/astropy/io/fits/card.py b/astropy/io/fits/card.py
--- a/astropy/io/fits/card.py
+++ b/astropy/io/fits/card.py
@@ -559,7 +559,7 @@ class Card(_Verify):
             # bytes for now; if it results in mojibake due to e.g. UTF-8
             # encoded data in a FITS header that's OK because it shouldn't be
             # there in the first place
-            image = image.decode('latin1')
+            image = image.decode('ascii')
 
         card._image = _pad(image)
         card._verified = False
diff --git a/astropy/io/fits/header.py b/astropy/io/fits/header.py
--- a/astropy/io/fits/header.py
+++ b/astropy/io/fits/header.py
@@ -399,7 +399,7 @@ class Header:
             CONTINUE = b'CONTINUE'
             END = b'END'
             end_card = END_CARD.encode('ascii')
-            sep = sep.encode('latin1')
+            sep = sep.encode('ascii')
             empty = b''
```

**分类**：🟡 语义浅层（保留）

**理由**：两处修改都是 `latin1` → `ascii`。`latin1` 与 `ascii` 对纯 ASCII 内容完全等价，但对字节值 128-255 的内容会有差异（`ascii` 解码会抛 `UnicodeDecodeError`）。修改位置处于关键的编码路径上，模拟了真实开发者的错误认知（"FITS 只支持 ASCII，所以用 ascii 解码就够了"）。虽然标准 FITS 文件的头部通常是纯 ASCII，但 golden patch 的注释明确说明需要 `latin1` 来容忍非标准字节，这是一个语义上有意义的差异。两处修改协调一致，比 B 组更难检测。

**最终 mutation**（与原相同）：
```diff
diff --git a/astropy/io/fits/card.py b/astropy/io/fits/card.py
--- a/astropy/io/fits/card.py
+++ b/astropy/io/fits/card.py
@@ -559,7 +559,7 @@ class Card(_Verify):
             # bytes for now; if it results in mojibake due to e.g. UTF-8
             # encoded data in a FITS header that's OK because it shouldn't be
             # there in the first place
-            image = image.decode('latin1')
+            image = image.decode('ascii')
 
         card._image = _pad(image)
         card._verified = False
diff --git a/astropy/io/fits/header.py b/astropy/io/fits/header.py
--- a/astropy/io/fits/header.py
+++ b/astropy/io/fits/header.py
@@ -399,7 +399,7 @@ class Header:
             CONTINUE = b'CONTINUE'
             END = b'END'
             end_card = END_CARD.encode('ascii')
-            sep = sep.encode('latin1')
+            sep = sep.encode('ascii')
             empty = b''
```

**变异语义**：将 `latin1` 编码替换为 `ascii`。对于纯 ASCII 的 FITS 头（标准情况）两者等价，简单测试通过。只有当 FITS 文件头包含字节值 ≥128 的非标准字节时，`ascii` 解码会抛 `UnicodeDecodeError`，而 `latin1` 会静默接受。

---

### Group B — 替换

**原 mutation**：
```diff
--- a/astropy/io/fits/header.py
+++ b/astropy/io/fits/header.py
@@ -390,7 +390,7 @@ class Header:
         # immediately at the separator
         require_full_cardlength = set(sep).issubset(VALID_HEADER_CHARS)
 
-        if isinstance(data, bytes):
+        if not isinstance(data, bytes):
```

**分类**：🟡 语义浅层（替换）——最弱

**理由**：`not isinstance` 翻转是最简单的条件取反，任何传入 bytes 的测试都会立即失败（bytes 变量被设为 str 类型，str 比较操作在 bytes 数据上立即出错）。这是同组内最孤立、最容易被检测的修改。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/io/fits/header.py b/astropy/io/fits/header.py
index 29d7a4f5d3..cd77f6ce79 100644
--- a/astropy/io/fits/header.py
+++ b/astropy/io/fits/header.py
@@ -398,7 +398,7 @@ class Header:
             # opportunity to display warnings later during validation
             CONTINUE = b'CONTINUE'
             END = b'END'
-            end_card = END_CARD.encode('ascii')
+            end_card = END_CARD
             sep = sep.encode('latin1')
             empty = b''
         else:
```

**变异语义**：在 bytes 分支中，`end_card` 被设置为 `str` 类型（`END_CARD`，即 `'END' + ' '*77`）而非 `bytes` 类型。在 Python 3 中，`bytes == str` 永远返回 `False`，因此 `next_image == end_card`（其中 `next_image` 是80字节的 bytes 块）永远不会触发，END 卡片永远无法被检测到。循环会继续处理 FITS 文件中 END 卡片之后的二进制数据（图像数据），导致解析出错误的卡片或抛出异常。通过了所有 str 输入的测试，只在 bytes 输入路径下失败。开发者容易犯的错误：忘记对比较字符串进行编码。

---

### Group C — 替换

**原 mutation**：
```diff
diff --git a/astropy/io/fits/header.py b/astropy/io/fits/header.py
--- a/astropy/io/fits/header.py
+++ b/astropy/io/fits/header.py
@@ -390,22 +390,10 @@ class Header:
         # immediately at the separator
         require_full_cardlength = set(sep).issubset(VALID_HEADER_CHARS)
 
-        if isinstance(data, bytes):\n             ...（整个bytes分支）
+        CONTINUE = 'CONTINUE'
+        END = 'END'
+        end_card = END_CARD
+        empty = ''
```

**分类**：🔴 必须替换

**理由**：直接删除整个 `if isinstance(data, bytes):` 分支，等同于还原 golden patch 在 `header.py` 中的核心修改。这是直接冗余，不符合高质量 mutation 的要求。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/io/fits/card.py b/astropy/io/fits/card.py
index 312f47b208..a3dafdb295 100644
--- a/astropy/io/fits/card.py
+++ b/astropy/io/fits/card.py
@@ -554,14 +554,13 @@ class Card(_Verify):
         """
 
         card = cls()
+        card._image = _pad(image)
         if isinstance(image, bytes):
             # FITS supports only ASCII, but decode as latin1 and just take all
             # bytes for now; if it results in mojibake due to e.g. UTF-8
             # encoded data in a FITS header that's OK because it shouldn't be
             # there in the first place
-            image = image.decode('latin1')
-
-        card._image = _pad(image)
+            card._image = card._image.decode('latin1')
         card._verified = False
         return card
```

**变异语义**：将 `_pad(image)` 调用移到 `isinstance` 检查之前。当 `image` 是 `bytes` 时，`_pad` 函数计算 `len(bytes_obj)` 正常，但随后执行 `bytes_obj + ' ' * (Card.length - strlen)`，在 Python 3 中 `bytes + str` 会抛 `TypeError: can't concat str to bytes`。这模拟了真实开发者的错误：先调用 `_pad` 再解码，没有意识到 `_pad` 内部有字符串拼接操作。对 str 输入完全无影响，只在 bytes 输入时立即失败。

---

### Group D — 替换

**原 mutation**：
```diff
--- a/astropy/io/fits/header.py
+++ b/astropy/io/fits/header.py
         # immediately at the separator
         require_full_cardlength = set(sep).issubset(VALID_HEADER_CHARS)
 
-        if isinstance(data, bytes):
-            ...（整个bytes分支）
+        CONTINUE = 'CONTINUE'
+        END = 'END'
+        end_card = END_CARD
+        empty = ''
```

**分类**：🔴 必须替换

**理由**：与 Group C 完全相同的 diff，重复冗余，必须替换。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/io/fits/header.py b/astropy/io/fits/header.py
index 29d7a4f5d3..51b9d25937 100644
--- a/astropy/io/fits/header.py
+++ b/astropy/io/fits/header.py
@@ -398,7 +398,7 @@ class Header:
             # opportunity to display warnings later during validation
             CONTINUE = b'CONTINUE'
             END = b'END'
-            end_card = END_CARD.encode('ascii')
+            end_card = END_CARD.encode('ascii').strip()
             sep = sep.encode('latin1')
             empty = b''
         else:
```

**变异语义**：`END_CARD.encode('ascii').strip()` 将 `b'END' + b' '*77`（80字节）去除尾部空格，得到 `b'END'`（3字节）。在循环中，`next_image`（80字节的 bytes 块）与3字节的 `b'END'` 比较，永远不相等，END 卡片无法被检测到。与 B 组不同：B 组是类型错误（str vs bytes），D 组是长度错误（80字节 vs 3字节）——两者模拟了不同的开发者错误（B：忘记编码；D：误以为只需比较关键字部分而忽略填充）。循环继续处理 END 卡片之后的数据，导致解析错误。

---

### Group E — 替换

**原 mutation**：
```diff
diff --git a/astropy/io/fits/card.py b/astropy/io/fits/card.py
--- a/astropy/io/fits/card.py
+++ b/astropy/io/fits/card.py
         if isinstance(image, bytes):
-            image = image.decode('latin1')
+            raise TypeError("Card.fromstring() does not accept bytes; use a string instead")
diff --git a/astropy/io/fits/header.py b/astropy/io/fits/header.py
         if isinstance(data, bytes):
-            ...（整个bytes分支）
+            raise TypeError("Header.fromstring() does not accept bytes; use a string instead")
```

**分类**：🔴 必须替换

**理由**：显式 `raise TypeError` 含有明显人工痕迹——错误信息直接说明"不接受 bytes"，与修复目的完全相反，代码审查时立即可见。属于不自然的 mutation。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/io/fits/header.py b/astropy/io/fits/header.py
index 29d7a4f5d3..c18018df87 100644
--- a/astropy/io/fits/header.py
+++ b/astropy/io/fits/header.py
@@ -400,7 +400,7 @@ class Header:
             END = b'END'
             end_card = END_CARD.encode('ascii')
             sep = sep.encode('latin1')
-            empty = b''
+            empty = ''
         else:
             CONTINUE = 'CONTINUE'
             END = 'END'
```

**变异语义**：在 bytes 分支中，`empty` 被设置为 `''`（str）而非 `b''`（bytes）。当执行 `empty.join(image)`（其中 `image` 是 bytes 块列表）时，`str.join(bytes_list)` 在 Python 3 中抛出 `TypeError: sequence item 0: expected str instance, bytes found`。即使 `image` 只有一个元素，`str.join([bytes_obj])` 仍然会报错（不同于 `b''.join([bytes_obj])` 正常工作）。这模拟了真实开发者的错误：在 bytes 分支中使用了 str 空字符串而非 bytes 空字符串作为连接符，视觉上 `''` 和 `b''` 极为相似，代码审查难以发现。

---

## 新设计 Mutation 说明

### Group B 新设计

**代码分析基础**：`Header.fromstring` 在 bytes 分支中设置5个局部变量，其中 `end_card` 是最关键的——它用于在 `require_full_cardlength=True`（默认情况）下检测 END 卡片。`END_CARD` 是模块级 str 常量 `'END' + ' '*77`，需要 `.encode('ascii')` 转为 bytes 才能与 bytes 数据块比较。

**选择位置的理由**：`end_card` 变量控制循环终止。如果 `end_card` 类型错误（str vs bytes），Python 3 的 `==` 比较不会报错，而是静默返回 `False`，导致循环不终止、解析继续到 FITS 文件的二进制数据区域。这种"静默失败"比立即报错更难检测。

**模拟的真实错误**：开发者在添加 bytes 分支时，忘记对 `END_CARD` 进行 `.encode()`，直接使用了已有的 str 常量。这是非常自然的遗漏，因为 `END_CARD` 在文件中已经定义，开发者可能没有意识到需要单独编码。

### Group C 新设计

**代码分析基础**：`Card.fromstring` 的正确顺序是：(1) 检测 bytes → 解码为 str；(2) 调用 `_pad(str)` 填充到80字符。`_pad` 内部使用 `input + ' ' * n`，要求 input 是 str。

**选择位置的理由**：将 `_pad(image)` 移到 `isinstance` 检查之前，看起来像是代码重构中的合理顺序调整（先初始化 `card._image`，再检查是否需要特殊处理）。但这破坏了 bytes 路径，因为 `_pad` 不能处理 bytes。对 str 路径完全无影响。

**模拟的真实错误**：开发者在重构代码时调整了语句顺序，没有意识到 `_pad` 依赖 str 类型。这是典型的"重构引入 bug"。

### Group D 新设计

**代码分析基础**：`end_card` 需要与80字节的 FITS 卡片块精确匹配。`END_CARD = 'END' + ' '*77` 是80字符的字符串，编码后是80字节。`.strip()` 去除尾部空格后只剩 `b'END'`（3字节）。

**选择位置的理由**：与 B 组互补——B 组是类型错误，D 组是内容错误（长度不匹配）。`END_CARD.encode('ascii').strip()` 看起来像是"规范化"操作，开发者可能误以为只需要比较关键字部分。

**模拟的真实错误**：开发者不了解 FITS 格式中 END 卡片必须是完整的80字节（`END` + 77个空格），误以为只需匹配 `END` 关键字本身。

### Group E 新设计

**代码分析基础**：`empty.join(image)` 中，`image` 是 bytes 块列表，`empty` 必须是 `b''`（bytes）。在 Python 3 中，`str.join(iterable)` 要求 iterable 中的所有元素都是 str，否则抛 TypeError。即使 `image` 只有一个元素，`''.join([b'data'])` 仍然报错。

**选择位置的理由**：`b''` 与 `''` 在视觉上极为相似，单字符差异（`b` 前缀），代码审查时容易忽略。这是5个变量中视觉差异最小的修改位置。

**模拟的真实错误**：开发者在 bytes 分支中设置 `empty` 变量时，习惯性地写了 `''` 而非 `b''`，没有注意到这个变量会用于 bytes 列表的 join 操作。这是 Python 2 → Python 3 迁移中的典型错误（Python 2 中 `''` 和 `b''` 等价）。
