# astropy__astropy-14598

## 问题背景

FITS Card 中对双单引号 `''` 的处理存在不一致性。在 FITS 标准中，`''` 是单引号 `'` 的转义表示。当字符串值较长（需要 CONTINUE 卡片）时，某些情况下 `''` 会被错误地转换为单个 `'`，导致 `Card.fromstring(str(card))` 后值发生变化，不满足幂等性。

Golden patch 修复了两处代码：
1. `_strg_comment_RE` 正则表达式缺少末尾 `$` 锚点，导致对 CONTINUE 卡片的字符串段匹配时在某些位置会匹配到字符串中间，把 `''` 误判为字符串结束后的内容。
2. `_split()` 方法中对 CONTINUE 卡片值片段多余地执行了 `.replace("''", "'")`，而正确的 `''` → `'` 解码应该由 `_parse_value()` 在最终组装后执行，此处双重解码导致 `''` 被错误地消费。

## Golden Patch 语义分析

**修复1（正则锚点）**：`_strg_comment_RE` 用于在 `_split()` 中匹配每个 CONTINUE 子卡片的 `"<value_fragment>& / <comment>"` 格式。没有 `$` 时，`re.match` 可以在字符串任意位置停止，当值片段末尾恰好有 `''` 时，regex 的非贪婪匹配会在 `''` 的第一个 `'` 处结束字符串组，把第二个 `'` 留在外面，导致 `strg` 组捕获不完整。加上 `$` 后，整个字符串必须完整匹配，迫使 regex 正确处理 `''`。

**修复2（移除错误替换）**：`_split()` 负责将多个 CONTINUE 子卡片拼接成一个逻辑值字符串，最终构造 `valuecomment = f"'{''.join(values)}' / ..."` 再交给 `_parse_value()` 解析。`_parse_value()` 中已经有 `re.sub("''", "'", m.group("strg"))` 来做 FITS 编码转义解码。若 `_split()` 中提前替换，则 `''` 在拼接前就变成了 `'`，`_parse_value()` 再也找不到 `''` 来替换，但实际上这个 `'` 是从 `''` 来的，所以最终值少了一个字符。

## 调用链分析

```
Card.fromstring(image_str)
  └─ Card._image = image_str, _verified = False

Card.value (property getter)
  └─ Card._parse_value()
       └─ Card._split()           ← 多卡片时调用 _itersubcards()
            └─ _itersubcards()    ← 逐个 80 字节子卡片
                 └─ Card._split() ← 单卡片的 keyword/valuecomment 分割
       └─ re.sub("''", "'", strg) ← FITS 引号解码（单卡片路径）

Card.image (property getter, 写路径)
  └─ Card._format_image()
       └─ Card._format_long_image()   ← 长字符串 CONTINUE 卡片生成
            └─ self._value.replace("'", "''")  ← FITS 引号编码
            └─ _words_group(value, 67)          ← 按67字符分块
```

数据流：`self._value`（Python 字符串，含原始 `'`）→ `_format_long_image` 编码为 `''` → 写入 FITS 卡片 → `_split()` 读取片段 → `_parse_value()` 解码 `''` → `'`。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | 直接还原 golden patch 第一处修改（移除 `$` 锚点） |
| B | 语义浅层 | 保留 | `==` → `!=` 反转 CONTINUE 续行标记逻辑，位于关键控制流节点 |
| C | 必须替换 | 替换 | 直接还原 golden patch 第二处修改（重新加入 `replace("''","'")` ） |
| D | 必须替换 | 替换 | 与 A 完全相同的 diff，重复冗余 |
| E | 必须替换 | 替换 | 功能等价于 C（`_replace_quotes_in_continue=True` 永远为真，效果完全相同） |

语义浅层共 1 个（B），替换其中最弱的 floor(1/2) = 0 个：无。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：
```diff
diff --git a/astropy/io/fits/card.py b/astropy/io/fits/card.py
index 89a5c0c0e..bd48eba60 100644
--- a/astropy/io/fits/card.py
+++ b/astropy/io/fits/card.py
@@ -66,7 +66,7 @@ class Card(_Verify):
     # followed by an optional comment
     _strg = r"\'(?P<strg>([ -~]+?|\'\'|) *?)\'(?=$|/| )"
     _comm_field = r"(?P<comm_field>(?P<sepr>/ *)(?P<comm>(.|\\n)*))"
-    _strg_comment_RE = re.compile(f"({_strg})? *{_comm_field}?$")
+    _strg_comment_RE = re.compile(f"({_strg})? *{_comm_field}?")
```
**分类**：🔴 必须替换

**理由**：这是 golden patch 第一处修改的精确逆操作，直接还原了已知 bug。代码审查者一眼就能看出这是在撤销修复，且与 Group D 完全相同，构成重复冗余。

**最终 mutation**（替换后）：
```diff
diff --git a/astropy/io/fits/card.py b/astropy/io/fits/card.py
index 89a5c0c0e8..efb47d3767 100644
--- a/astropy/io/fits/card.py
+++ b/astropy/io/fits/card.py
@@ -1057,7 +1057,7 @@ class Card(_Verify):
         output = []
 
         # do the value string
-        value = self._value.replace("'", "''")
+        value = self._value.replace("'", "'")
         words = _words_group(value, value_length)
         for idx, word in enumerate(words):
             if idx == 0:
```
**变异语义**：`_format_long_image` 在生成 CONTINUE 卡片时，FITS 标准要求将值中的 `'` 编码为 `''`。此 mutation 将编码操作变成无操作（`replace("'","'")`），导致含单引号的长字符串写入 FITS 时不被正确转义。读取时 `_parse_value()` 会对 `''` 解码，但写入时已无 `''`，故含 `'` 的值在 round-trip 后会丢失引号或解析错误。短字符串（不走 CONTINUE 路径）不受影响，通过大多数简单测试；只有含 `'` 的长字符串才失败，与 F2P 测试场景完全吻合。

---

### Group B — 保留
**原 mutation**：
```diff
diff --git a/astropy/io/fits/card.py b/astropy/io/fits/card.py
index 89a5c0c0e..22dae449c 100644
--- a/astropy/io/fits/card.py
+++ b/astropy/io/fits/card.py
@@ -860,7 +860,7 @@ class Card(_Verify):
 
                 value = m.group("strg") or ""
                 value = value.rstrip()
-                if value and value[-1] == "&":
+                if value and value[-1] != "&":
                     value = value[:-1]
                 values.append(value)
                 comment = m.group("comm")
```
**分类**：🟡 语义浅层（保留）

**理由**：`&` 是 CONTINUE 卡片的续行标记，`value[-1] == "&"` 检查当前片段是否需要继续拼接（去掉 `&` 后继续）。将 `==` 改为 `!=` 会反转逻辑：有 `&` 时不去掉（导致最终值含 `&`），无 `&` 时反而去掉最后一个字符（截断最后片段）。这个修改位于 CONTINUE 拼接的核心控制流上，能模拟开发者对续行标记语义的误解，会导致 F2P 测试中长字符串 round-trip 失败，且代码表面看起来像是在做"不同的条件判断"而非明显错误。值得保留。

**最终 mutation**（与原相同）：
```diff
diff --git a/astropy/io/fits/card.py b/astropy/io/fits/card.py
index 89a5c0c0e..22dae449c 100644
--- a/astropy/io/fits/card.py
+++ b/astropy/io/fits/card.py
@@ -860,7 +860,7 @@ class Card(_Verify):
 
                 value = m.group("strg") or ""
                 value = value.rstrip()
-                if value and value[-1] == "&":
+                if value and value[-1] != "&":
                     value = value[:-1]
                 values.append(value)
                 comment = m.group("comm")
```
**变异语义**：反转续行标记检测逻辑。所有 CONTINUE 长字符串 round-trip 都会失败（末尾片段被截断，中间片段保留 `&`）。短字符串测试通过。

---

### Group C — 替换
**原 mutation**：
```diff
diff --git a/astropy/io/fits/card.py b/astropy/io/fits/card.py
index 89a5c0c0e..70578eeee 100644
--- a/astropy/io/fits/card.py
+++ b/astropy/io/fits/card.py
@@ -859,7 +859,7 @@ class Card(_Verify):
                     return kw, vc
 
                 value = m.group("strg") or ""
-                value = value.rstrip()
+                value = value.rstrip().replace("''", "'")
                 if value and value[-1] == "&":
                     value = value[:-1]
                 values.append(value)
```
**分类**：🔴 必须替换

**理由**：这是 golden patch 第二处修改的精确逆操作，直接还原了已知 bug。

**最终 mutation**（替换后）：
```diff
diff --git a/astropy/io/fits/card.py b/astropy/io/fits/card.py
index 89a5c0c0e8..1cd615a84c 100644
--- a/astropy/io/fits/card.py
+++ b/astropy/io/fits/card.py
@@ -768,7 +768,7 @@ class Card(_Verify):
         if m.group("bool") is not None:
             value = m.group("bool") == "T"
         elif m.group("strg") is not None:
-            value = re.sub("''", "'", m.group("strg"))
+            value = re.sub("''", "", m.group("strg"))
         elif m.group("numr") is not None:
             #  Check for numbers with leading 0s.
             numr = self._number_NFSC_RE.match(m.group("numr"))
```
**变异语义**：`_parse_value()` 中 FITS 引号解码从 `''` → `'` 改为 `''` → `""`（直接删除）。这影响所有含单引号的字符串值（无论长短），但由于大多数测试用例不含 `'`，简单测试会通过。含 `'` 的任何字符串在 `Card.fromstring` 后 `value` 将丢失所有单引号，F2P 测试（含 `''` 的长字符串）必然失败。修改位置在与 golden patch 不同的函数（`_parse_value` vs `_split`），且从代码表面看像是"把空字符串替换为 `'`" 的错误，而非明显的逻辑反转。

---

### Group D — 替换
**原 mutation**：
```diff
diff --git a/astropy/io/fits/card.py b/astropy/io/fits/card.py
index 89a5c0c0e..bd48eba60 100644
--- a/astropy/io/fits/card.py
+++ b/astropy/io/fits/card.py
@@ -66,7 +66,7 @@ class Card(_Verify):
     # followed by an optional comment
     _strg = r"\'(?P<strg>([ -~]+?|\'\'|) *?)\'(?=$|/| )"
     _comm_field = r"(?P<comm_field>(?P<sepr>/ *)(?P<comm>(.|\\n)*))"
-    _strg_comment_RE = re.compile(f"({_strg})? *{_comm_field}?$")
+    _strg_comment_RE = re.compile(f"({_strg})? *{_comm_field}?")
```
**分类**：🔴 必须替换（与 Group A 完全相同，重复冗余）

**理由**：与 A 的 diff 完全相同，5 个 mutation 中出现重复是不可接受的。

**最终 mutation**（替换后）：
```diff
diff --git a/astropy/io/fits/card.py b/astropy/io/fits/card.py
index 89a5c0c0e8..5e9037162c 100644
--- a/astropy/io/fits/card.py
+++ b/astropy/io/fits/card.py
@@ -1052,7 +1052,7 @@ class Card(_Verify):
         if self.keyword in Card._commentary_keywords:
             return self._format_long_commentary_image()
 
-        value_length = 67
+        value_length = 68
         comment_length = 64
         output = []
 
```
**变异语义**：`_format_long_image` 用 `value_length=67` 分块是精确计算的：FITS 卡片 80 字节 = 10 字节关键字头 + `'` + 67 字节内容 + `&'` = 80。改为 68 后，每块内容多 1 字节，整个卡片行变为 81 字节，超过 FITS 标准的 80 字节限制。读取时 `_itersubcards()` 按 80 字节切分，会在错误位置切断，导致最后几个字符被移到下一个子卡片，引起解析错误。短字符串（不需要 CONTINUE）不受影响，只有需要 CONTINUE 的长字符串才失败。这个 off-by-one 错误模拟了开发者在计算 FITS 卡片布局时的边界计算失误。

---

### Group E — 替换
**原 mutation**：
```diff
diff --git a/astropy/io/fits/card.py b/astropy/io/fits/card.py
index 89a5c0c0e..cf996076b 100644
--- a/astropy/io/fits/card.py
+++ b/astropy/io/fits/card.py
@@ -42,6 +42,9 @@ class Card(_Verify):
     length = CARD_LENGTH
     """The length of a Card image; should always be 80 for valid FITS files."""
 
+    # Control whether to replace double quotes with single quotes in CONTINUE cards
+    _replace_quotes_in_continue = True
+
     ...
@@ -860,6 +863,8 @@ class Card(_Verify):
 
                 value = m.group("strg") or ""
                 value = value.rstrip()
+                if self._replace_quotes_in_continue:
+                    value = value.replace("''", "'")
                 if value and value[-1] == "&":
```
**分类**：🔴 必须替换（功能等价冗余：`_replace_quotes_in_continue = True` 永远为真，效果等同于 C）

**理由**：虽然写法不同，但由于类属性默认为 `True` 且无任何代码修改它，`if self._replace_quotes_in_continue:` 永远执行，行为与 C 完全等价。此外代码注释 "Control whether to replace double quotes" 暗示了这是一个刻意设计的 bug 开关，不自然。

**最终 mutation**（替换后）：
```diff
diff --git a/astropy/io/fits/card.py b/astropy/io/fits/card.py
index 89a5c0c0e8..948fdfcd96 100644
--- a/astropy/io/fits/card.py
+++ b/astropy/io/fits/card.py
@@ -871,7 +871,7 @@ class Card(_Verify):
                 valuecomment = "".join(values)
             else:
                 # CONTINUE card
-                valuecomment = f"'{''.join(values)}' / {' '.join(comments)}"
+                valuecomment = f"'{''.join(values)}' / {''.join(comments)}"
             return keyword, valuecomment
 
         if self.keyword in self._special_keywords:
```
**变异语义**：`_split()` 将多个 CONTINUE 子卡片的注释片段拼接时，从 `' '.join(comments)`（空格分隔）改为 `''.join(comments)`（无分隔符）。对于只有一个注释片段的 CONTINUE 卡片，行为完全相同（通过大多数测试）。只有当注释被拆分成多个 CONTINUE 卡片时（注释极长），拼接后的注释会缺少单词间空格。F2P 测试中含注释的长字符串 round-trip 会发现注释内容变化。这个 bug 模拟了开发者在拼接字符串时忘记分隔符的真实错误，且只在特定输入（多注释片段）下暴露。

## 新设计 Mutation 说明

### Mutation A（替换）：`_format_long_image` 引号编码失效
- **代码分析**：`_format_long_image` 在写入 CONTINUE 卡片前必须将 `'` 编码为 `''`（FITS 标准）。这个编码步骤是 `_parse_value` 中解码步骤的对称操作。
- **选择位置**：`value = self._value.replace("'", "''")` → `replace("'", "'")`（无操作）
- **模拟错误**：开发者误以为 `replace("'", "'")` 是"保留单引号不变"的操作，实际上应该是 `replace("'", "''")`。
- **检测难度**：不含 `'` 的长字符串完全不受影响；只有含 `'` 的长字符串在 round-trip 时失败。

### Mutation C（替换）：`_parse_value` 引号解码清除而非替换
- **代码分析**：`_parse_value` 中 `re.sub("''", "'", ...)` 是 FITS 引号解码的正确实现。
- **选择位置**：将替换目标从 `"'"` 改为 `""`，即删除 `''` 而非转换为 `'`。
- **模拟错误**：开发者误以为 `''` 是需要清除的转义序列，而非需要还原为 `'` 的编码。

### Mutation D（替换）：`_format_long_image` 分块长度 off-by-one
- **代码分析**：`value_length = 67` 是精确计算的 FITS 布局常量。CONTINUE 卡片格式：`CONTINUE  '<67chars>&'` = 10+1+67+2 = 80 字节。
- **选择位置**：`value_length = 67` → `value_length = 68`
- **模拟错误**：开发者在计算 FITS 卡片布局时犯了 off-by-one 错误，忘记了 `&'` 占 2 字节而非 1 字节。

### Mutation E（替换）：`_split` 注释拼接缺少分隔符
- **代码分析**：`_split()` 最终构造 `valuecomment` 时，多个注释片段需要用空格连接以保持可读性。
- **选择位置**：`' '.join(comments)` → `''.join(comments)`
- **模拟错误**：开发者在拼接列表时忘记了分隔符，这是一个极其常见的真实错误。
- **检测难度**：注释只有一个片段时完全不受影响（`join` 单元素列表结果相同），只有注释极长被拆分时才暴露。
