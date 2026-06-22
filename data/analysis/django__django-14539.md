# django__django-14539

## 问题背景

`urlize()` 处理含 HTML 转义实体且带尾随标点的字符串时，尾随标点裁剪错误。例如 `urlize('Search for google.com/?q=1&lt! and see.')` 期望把 `&lt` 留在链接文本内、只把 `!` 移到链接外，但旧实现因为在 unescaped 字符串上算长度、却在原始 `middle`（含 `&lt` 这种多字符实体）上做切片，索引错位，把 `lt` 重复输出（`...&lt</a>lt!...`）。Golden patch 在 `trim_punctuation` 中先算 `punctuation_count = len(middle_unescaped) - len(stripped)`（真实被剥离的标点数），再用**负索引** `middle[-punctuation_count:]` / `middle[:-punctuation_count]` 从原始 `middle` 尾部精确切走这些标点。

## Golden Patch 语义分析

```python
middle_unescaped = html.unescape(middle)
stripped = middle_unescaped.rstrip(TRAILING_PUNCTUATION_CHARS)
if middle_unescaped != stripped:
    punctuation_count = len(middle_unescaped) - len(stripped)
    trail = middle[-punctuation_count:] + trail
    middle = middle[:-punctuation_count]
    trimmed_something = True
```
核心语义：**被剥离的标点数量 `punctuation_count` 必须在 unescaped 串上计算（标点本身不含实体，长度差即真实标点数），而切片必须用负索引作用于原始 `middle`**。这样无论 `middle` 里有多少多字符实体（`&lt` 等），都只从尾部切走 `punctuation_count` 个真实标点字符，实体保持完整留在 `middle` 内。旧代码用 `middle[len(stripped):]` 这种正索引把 unescaped 的长度当作原始串的下标，实体长度差导致错位。

F2P 测试 `TestUtilsHtml.test_urlize` 新增用例 `'Search for google.com/?q=1&lt! and see.'` → `'...google.com/?q=1&lt</a>! and see.'`，断言 `&lt` 不被破坏、`!` 正确移出。

## 调用链分析

`urlize` → 内部 `trim_punctuation(lead, middle, trail)` 闭包。`middle` 是候选 URL 文本（可能含 HTML 实体），`trail` 是尾随要移出链接的部分。裁剪逻辑先处理包裹标点（括号引号），再 unescape 处理尾随标点。`punctuation_count`、切片的源（`middle` vs `stripped` 下标）、切片方向（负索引 vs 正索引）三者必须一致，任一错位都会破坏实体或多/少切字符。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `len(middle_unescaped)`→`len(middle)`，计数源改成含实体的原始串 |
| B | ➕ 补充 | 新增 | 原缺 B 组；条件判断比较错对象（`middle != stripped`） |
| C | 🟢 高质量 | 保留 | 切片改用正索引 `middle[len(stripped):]`，实体长度差致错位 |
| D | 🟢 高质量 | 保留 | trail 切片源用 `-len(stripped)` 而非 `-punctuation_count` |
| E | ➕ 补充 | 新增 | 原缺 E 组；把正确切片藏到默认关闭的开关后 |

原实例只有 A/C/D 三组，缺 B、E。补充 B、E，A/C/D 各为不同机制故全部保留。

## 各组 Mutation 分析

### Group A — 保留（A1 计数源错误）
```diff
-                punctuation_count = len(middle_unescaped) - len(stripped)
+                punctuation_count = len(middle) - len(stripped)
```
**变异语义**：`punctuation_count` 改用原始 `middle`（含实体）长度减 `stripped`（unescaped 已剥离）长度。当 `middle` 含多字符实体时，`len(middle) > len(middle_unescaped)`，算出的 count 偏大，切走过多字符，破坏实体。典型无实体输入两长度相等故能通过，只有含实体场景失败。保留。

### Group B — 补充（B3 条件比较错对象）
```diff
-            if middle_unescaped != stripped:
+            if middle != stripped:
```
**变异语义**：进入裁剪分支的判据从"unescaped 串是否被剥离了标点"改成"原始 middle 是否等于 unescaped-stripped"。含实体时 `middle`（带 `&lt`）几乎永远 `!= stripped`，故即便没有真正尾随标点也会误进分支并执行负索引切片；而对纯标点场景判定可能与原意不符。条件对象错配导致裁剪触发时机错乱，F2P 用例输出错误。

### Group C — 保留（C1 切片形状：正索引）
```diff
-                trail = middle[-punctuation_count:] + trail
-                middle = middle[:-punctuation_count]
+                trail = middle[len(stripped):] + trail
+                middle = middle[:len(stripped)]
```
**变异语义**：用 `len(stripped)`（unescaped 串的长度）作为原始 `middle` 的正向切片下标。含实体时 unescaped 比原始短，`middle[len(stripped):]` 的切点落在错误位置，把实体字符切进 trail。还原了旧 bug 的核心错位。保留。

### Group D — 保留（D1 切片源不一致）
```diff
-                trail = middle[-punctuation_count:] + trail
+                trail = middle[-len(stripped):] + trail
```
**变异语义**：只改 trail 的负索引量，从 `punctuation_count` 改成 `len(stripped)`。`middle = middle[:-punctuation_count]` 仍用正确量，导致 trail 与 middle 的切分点不一致——trail 取了 `len(stripped)` 个尾字符（通常远多于真实标点），middle 与 trail 内容重叠或错位。模拟"两处该用同一变量却写错其一"。保留。

### Group E — 补充（E2 隐式→显式开关）
```diff
                 punctuation_count = len(middle_unescaped) - len(stripped)
-                trail = middle[-punctuation_count:] + trail
-                middle = middle[:-punctuation_count]
+                if getattr(html, "_entity_aware_urlize", False):
+                    trail = middle[-punctuation_count:] + trail
+                    middle = middle[:-punctuation_count]
+                else:
+                    trail = middle[len(stripped):] + trail
+                    middle = middle[:len(stripped) - len(middle_unescaped)]
```
**变异语义**：把正确的负索引切片藏到模块级开关 `html._entity_aware_urlize` 后，默认 `False` → 走 else 分支的旧 buggy 正索引逻辑。只有显式设置该属性才恢复正确行为。模拟"把修复做成可配置、默认却保留旧行为"。

## 新设计 Mutation 说明

原实例仅含 A/C/D（均针对 `punctuation_count`/切片的不同写法），缺 B、E。补充：B 在更上游的条件判断处比较错对象（`middle != stripped`），使裁剪触发时机错乱；E 把正确切片 gate 在默认关闭的开关后退回旧逻辑。五组覆盖"计数源 / 条件对象 / 正索引切片 / 切片源不一致 / 默认关闭开关"五个角度。全部实测：golden 通过、五个变异均令 F2P（`test_urlize`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
