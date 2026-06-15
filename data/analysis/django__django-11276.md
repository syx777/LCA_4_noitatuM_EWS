# django__django-11276

## 问题背景

该 issue 要求将 `django.utils.html.escape()` 中自定义的字符转义逻辑替换为 Python 标准库的 `html.escape()`。原实现使用一个 `_html_escapes` 字典通过 `str.translate()` 映射 5 个特殊字符（`&`, `<`, `>`, `"`, `'`），其中单引号被编码为 `&#39;`。Python 标准库的 `html.escape()` 将单引号编码为 `&#x27;`（十六进制形式），二者都是合法的 HTML 实体，但编码方式不同。

Golden patch 同时还：
1. 移除了 `urlize()` 中的内嵌 `unescape()` 函数，改用标准库 `html.unescape()`
2. 更新了大量测试文件中的期望字符串（`&#39;` → `&#x27;`）

## Golden Patch 语义分析

核心修复包含两个语义变更：

**变更1**：`escape()` 函数实现替换
- 原：`return mark_safe(str(text).translate(_html_escapes))` — 单引号 → `&#39;`
- 新：`return mark_safe(html.escape(str(text)))` — 单引号 → `&#x27;`
- 为什么正确：使用社区维护的标准库，性能更好，编码方式更规范

**变更2**：`urlize()` 中 `unescape()` 内嵌函数替换
- 原：手动 `.replace()` 链，只处理 5 个命名实体（包括 `&#39;` → `'`）
- 新：`html.unescape(middle)` — 处理所有 HTML 实体（包括数字和十六进制形式）
- 为什么正确：标准库 `html.unescape()` 支持所有合法 HTML 实体，覆盖面更广

## 调用链分析

```
escape(text)
  ├── 被 conditional_escape(text) 调用（非 SafeData 时）
  │     ├── 被 format_html(format_string, *args, **kwargs) 调用
  │     │     └── 被 format_html_join() 调用
  │     └── 被 linebreaks() 调用（autoescape=True 时）
  └── 被 urlize() 中 autoescape 分支直接调用

html.unescape(middle)  [替换了原来的内嵌 unescape()]
  ├── 被 trim_punctuation() 调用：用于正确识别 HTML 编码 URL 末尾的标点
  └── 被 URL 生成逻辑调用：在传给 smart_urlquote() 前解码 HTML 实体
```

数据流：用户输入文本 → `escape()` → HTML 实体编码 → `mark_safe()` 包装 → 输出安全字符串

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟡 语义浅层 | 保留 | 在关键 escape() 函数中，quote=False 禁用双引号转义，影响属性安全 |
| B | 🟡 语义浅层 | 保留 | 去掉 str() 会导致非字符串输入（如懒惰字符串）在某些情况下出错 |
| C | — | 新建 | 原始数据中缺少 C 组，需从零设计 |
| D | 🔴 必须替换 | 替换 | 与 A 组 diff 完全相同，直接重复 |
| E | 🟡 语义浅层 | 替换 | 效果与 A/D 相似（也是 quote=False），且 A 已保留一个同类 |

语义浅层共 3 个（A、B、E），替换其中最弱的 floor(3/2)=1 个：**E 组**（与 A 组效果高度相似）。

加上必须替换 D 组，共替换 2 组（D、E），新建 1 组（C）。

## 各组 Mutation 分析

### Group A — 保留
**原 mutation**：
```diff
diff --git a/django/utils/html.py b/django/utils/html.py
index b26cbd16b8..a094624c0a 100644
--- a/django/utils/html.py
+++ b/django/utils/html.py
@@ -36,7 +36,7 @@ def escape(text):
     This may result in double-escaping. If this is a concern, use
     conditional_escape() instead.
     """
-    return mark_safe(html.escape(str(text)))
+    return mark_safe(html.escape(str(text), quote=False))
 
 
 _js_escapes = {
```
**分类**：🟡 语义浅层（保留）
**理由**：`quote=False` 禁用 `"` 和 `'` 的转义，会导致含引号的用户内容在 HTML 属性中产生 XSS 漏洞。虽然是单行改动，但位置在整个 Django HTML 转义的核心函数，影响所有下游调用链。修改在关键逻辑节点上，能模拟真实的"认为引号不需要转义"的错误假设。
**最终 mutation**：同原始 diff

**变异语义**：`quote=False` 时，`"` 和 `'` 不会被转义为 `&quot;` / `&#x27;`，导致含引号的内容在 HTML 属性值中可破坏属性边界。能通过不涉及引号的测试，在属性值转义测试（如 `test_urlize` 中的 wrapping chars）下失败。

---

### Group B — 保留
**原 mutation**：
```diff
diff --git a/django/utils/html.py b/django/utils/html.py
index b26cbd16b8..9ba7f9cb11 100644
--- a/django/utils/html.py
+++ b/django/utils/html.py
@@ -36,7 +36,7 @@ def escape(text):
     This may result in double-escaping. If this is a concern, use
     conditional_escape() instead.
     """
-    return mark_safe(html.escape(str(text)))
+    return mark_safe(html.escape(text))
 
 
 _js_escapes = {
```
**分类**：🟡 语义浅层（保留）
**理由**：去掉 `str()` 强转意味着当 `text` 不是字符串时（如整数、懒惰翻译字符串 `Promise` 对象），`html.escape()` 会直接接收非字符串。`html.escape()` 本身对非字符串输入会抛 `AttributeError`（它调用 `.replace()`）。但 `@keep_lazy` 装饰器会将 Promise 对象包装后传入，在某些场景下行为未定义。这是一个真实开发者可能犯的"多余代码删除"型错误。
**最终 mutation**：同原始 diff

**变异语义**：非字符串输入会导致运行时错误或异常行为。能通过所有传入字符串的测试，在传入非字符串（如数字、懒惰字符串对象）时失败。

---

### Group C — 新建
**原 mutation**：（缺失，新设计）
**分类**：新建高质量 mutation

**最终 mutation**：
```diff
diff --git a/django/utils/html.py b/django/utils/html.py
index b26cbd16b8..e413b10ca0 100644
--- a/django/utils/html.py
+++ b/django/utils/html.py
@@ -276,7 +276,7 @@ def urlize(text, trim_url_limit=None, nofollow=False, autoescape=False):
             # Trim trailing punctuation (after trimming wrapping punctuation,
             # as encoded entities contain ';'). Unescape entites to avoid
             # breaking them by removing ';'.
-            middle_unescaped = html.unescape(middle)
+            middle_unescaped = middle
             stripped = middle_unescaped.rstrip(TRAILING_PUNCTUATION_CHARS)
             if middle_unescaped != stripped:
                 trail = middle[len(stripped):] + trail
```

**变异语义**：`trim_punctuation()` 中跳过对 `middle` 的 HTML 解码，直接用编码后的字符串进行尾部标点检测。由于 HTML 编码的实体含有 `;` 字符（如 `&amp;`、`&#x27;`），而 TRAILING_PUNCTUATION_CHARS（`.,:;!`）包含 `;`，这会导致编码实体末尾的 `;` 被误判为需要裁剪的标点，破坏 URL 中含特殊字符的处理逻辑。这个错误在 `urlize` 测试中才会暴露，大多数简单字符串测试能正常通过。

---

### Group D — 替换
**原 mutation**：
```diff
diff --git a/django/utils/html.py b/django/utils/html.py
index b26cbd16b8..a094624c0a 100644
--- a/django/utils/html.py
+++ b/django/utils/html.py
@@ -36,7 +36,7 @@ def escape(text):
     This may result in double-escaping. If this is a concern, use
     conditional_escape() instead.
     """
-    return mark_safe(html.escape(str(text)))
+    return mark_safe(html.escape(str(text), quote=False))
```
**分类**：🔴 必须替换
**理由**：与 Group A 的 diff 完全相同（相同的 index hash `b26cbd16b8..a094624c0a`，相同的修改内容）。这是直接重复，必须替换为不同位置的高质量 mutation。

**最终 mutation（替换后）**：
```diff
diff --git a/django/utils/html.py b/django/utils/html.py
index b4a546df46..440e30e093 100644
--- a/django/utils/html.py
+++ b/django/utils/html.py
@@ -92,12 +92,11 @@ def conditional_escape(text):
     This function relies on the __html__ convention used both by Django's
     SafeData class and by third-party libraries like markupsafe.
     """
-    if isinstance(text, Promise):
-        text = str(text)
     if hasattr(text, '__html__'):
         return text.__html__()
-    else:
-        return escape(text)
+    if isinstance(text, Promise):
+        text = str(text)
+    return escape(text)
 
 
 def format_html(format_string, *args, **kwargs):
```

**变异语义**：将 `conditional_escape()` 中的检查顺序颠倒：先检查 `__html__`，再检查 `Promise`。这导致：实现了 `__html__()` 方法的 `Promise` 懒惰字符串会直接通过 `__html__()` 返回，而不经过 `str()` 转换。在 Django 的国际化场景中，`Promise` 对象实现了 `__html__` 并可能返回未经安全处理的内容，导致 XSS 风险。这模拟了开发者"重构条件分支时误改逻辑顺序"的真实错误，代码结构看起来合理，但语义上改变了 Promise 对象的处理路径。

---

### Group E — 替换
**原 mutation**：
```diff
diff --git a/django/utils/html.py b/django/utils/html.py
index b26cbd16b8..b24b7ec163 100644
--- a/django/utils/html.py
+++ b/django/utils/html.py
@@ -27,7 +27,7 @@ simple_url_2_re = re.compile(r'^www\.|^(?!http)\w[^@]+\.(com|edu|gov|int|mil|net
 
 
 @keep_lazy(str, SafeString)
-def escape(text):
+def escape(text, quote=False):
...
-    return mark_safe(html.escape(str(text)))
+    return mark_safe(html.escape(str(text), quote=quote))
```
**分类**：🟡 语义浅层（替换）
**理由**：虽然修改了函数签名（多行），但核心效果与 Group A 完全相同（`quote=False` 时不转义引号）。由于 `escape()` 的所有调用者都不传 `quote` 参数，`quote=False` 始终生效，与 Group A 行为等价。已保留 Group A，此组效果重复，应替换为针对不同代码路径的 mutation。

**最终 mutation（替换后）**：
```diff
diff --git a/django/utils/html.py b/django/utils/html.py
index b4a546df46..1d686cea0c 100644
--- a/django/utils/html.py
+++ b/django/utils/html.py
@@ -106,8 +106,8 @@ def format_html(format_string, *args, **kwargs):
     and call mark_safe() on the result. This function should be used instead
     of str.format or % interpolation to build up small HTML fragments.
     """
-    args_safe = map(conditional_escape, args)
-    kwargs_safe = {k: conditional_escape(v) for (k, v) in kwargs.items()}
+    args_safe = map(escape, args)
+    kwargs_safe = {k: escape(v) for (k, v) in kwargs.items()}
     return mark_safe(format_string.format(*args_safe, **kwargs_safe))
```

**变异语义**：`format_html()` 改用 `escape()` 代替 `conditional_escape()`，导致已经标记为安全的 `SafeData`/`SafeString` 输入会被**重复转义**（double-escape）。例如，传入 `mark_safe('<b>text</b>')` 给 `format_html` 时，`<` 会被转义为 `&lt;`，原本安全的 HTML 标签变成字面文本。这模拟了开发者"认为统一使用 `escape()` 比 `conditional_escape()` 更安全"的误判。在涉及 `SafeData` 输入的模板渲染测试中失败，而普通字符串输入的测试能通过。

## 新设计 Mutation 说明

### Group C 设计说明

**基于的代码分析**：
- Golden patch 同时修改了 `urlize()` 中的内嵌 `unescape()` 函数，将手动 `.replace()` 链替换为 `html.unescape()`
- `trim_punctuation()` 中使用 `html.unescape(middle)` 的目的：在检测尾部标点前先解码 HTML 实体，避免将 `&amp;` 中的 `;` 误判为标点
- `TRAILING_PUNCTUATION_CHARS = '.,:;!'` 包含分号 `;`

**为什么选择这个位置**：
- 针对 golden patch 的另一个修改点（`unescape()` → `html.unescape()`），而不是所有其他 mutation 都集中在的 `escape()` 函数
- 修改 `middle_unescaped = middle`（跳过解码）是真实开发者可能犯的"性能优化"错误：认为 URL 不会含有 HTML 编码字符，直接跳过解码步骤

**模拟的真实开发者错误**：
- 开发者认为 `html.unescape()` 调用是多余的（URL 应该是原始字符串），或者认为该解码步骤应该发生在其他地方
- 错误会在含有 HTML 编码字符的 URL 处理中才暴露（如 `urlize` 的 `test_url_split_chars` 等测试）

### Group D 设计说明

**基于的代码分析**：
- `conditional_escape()` 是 `escape()` 的包装器，处理三种情况：Promise 懒惰字符串、有 `__html__` 方法的对象、普通字符串
- 原始代码先将 Promise 转为 str，再检查 `__html__`；这确保了 Promise 对象即使有 `__html__` 方法也会被正确字符串化
- Django 的国际化功能大量使用 Promise 懒惰翻译字符串

**为什么选择这个位置**：
- 修改 `conditional_escape()` 而非 `escape()` 本身，针对不同的代码路径
- 检查顺序颠倒看起来是合理的重构（"应该先检查最具体的协议 `__html__`"），但实际上改变了 Promise+`__html__` 对象的行为

**模拟的真实开发者错误**：
- 开发者重构条件分支时，认为"应该先检查 `__html__` 协议因为它更通用"，不了解 Promise+`__html__` 组合的特殊语义

### Group E 设计说明

**基于的代码分析**：
- `format_html()` 是 Django 模板系统中构建 HTML 片段的核心工具函数
- 使用 `conditional_escape()` 而非 `escape()` 的原因：防止 `SafeData` 被重复转义
- 大量 Django 内部代码（如 `json_script()`、各种 widget 的 `render()` 方法）使用 `format_html()`

**为什么选择这个位置**：
- 针对 `escape()` 的间接调用路径，而非直接修改 `escape()` 本身
- 在整个调用链中引入错误，根因（`format_html` 使用了错误的转义函数）与表现（模板输出中的 HTML 字面量被再次转义）有距离

**模拟的真实开发者错误**：
- 开发者认为"用更严格的 `escape()` 替代 `conditional_escape()` 更安全"，不了解 `SafeData` 的约定
- 这种错误在代码审查时看起来像是"防御性加固"，实则破坏了 SafeString 的语义契约
