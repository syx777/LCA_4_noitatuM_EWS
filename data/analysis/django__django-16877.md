# django__django-16877

## 问题背景

新增模板过滤器 `escapeseq`——它对序列的作用相当于 `escape` 之于 `safe`（对应已有的 `safeseq`）。用法如 `{{ some_list|escapeseq|join:"," }}`：在 join 之前对列表每个元素逐个转义。该过滤器在 autoescape 关闭的上下文中尤其有用。Golden patch 在 `defaultfilters.py` 新增 `escapeseq` 过滤器：`return [conditional_escape(obj) for obj in value]`——对序列每个元素调 `conditional_escape`（已 mark_safe 的不重复转义），返回结果列表。

## Golden Patch 语义分析

```python
@register.filter(is_safe=True)
def escapeseq(value):
    """
    An "escape" filter for sequences. Mark each element in the sequence,
    individually, as a string that should be auto-escaped. Return a list with
    the results.
    """
    return [conditional_escape(obj) for obj in value]
```
核心语义：**逐元素（`for obj in value`）调用 `conditional_escape` 并返回一个 list**。三个要点：(1) `conditional_escape` 而非 `escape`——对已标记 safe（`mark_safe`）的元素不重复转义；(2) 列表推导逐元素处理而非对整个序列一次性处理；(3) 返回 list 以便后续 `join` 等过滤器逐项操作。`conditional_escape` 保证 safe 元素原样保留，普通字符串中的 `&`/`<`/`>` 被转义。

F2P 测试模块 `template_tests.filter_tests.test_escapeseq`（4 个用例）：`test_basic`（普通元素被转义、mark_safe 元素原样）、`test_autoescape_off`（autoescape off 下行为一致）、`test_chain_join`、`test_chain_join_autoescape_off`。

## 调用链分析

模板渲染 `{{ a|escapeseq|join:", " }}` → `escapeseq(a)` 返回逐元素 `conditional_escape` 后的 list → `join` 过滤器把各元素用分隔符连接。`conditional_escape` 对 `SafeString`（有 `__html__`）原样返回、对普通 str 转义。若改用 `escape`（强制转义 safe）、过滤掉 safe 元素、对整序列而非逐元素处理、原样返回不转义、或藏到开关后，都会让 F2P 断言的输出不符。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `conditional_escape`→`escape`，safe 元素被二次转义 |
| B | 🟢 高质量 | 保留 | 列表推导加 `if not hasattr(obj, "__html__")`，safe 元素被丢弃 |
| C | 🟢 高质量 | 保留 | `conditional_escape(value)` 对整序列而非逐元素，返回非 list |
| D | ➕ 补充 | 新增 | `return list(value)`，原样拷贝不转义 |
| E | 🟢 高质量 | 保留 | 转义藏到 `force_escape_safe` 参数后（默认走 `escape`） |

原始 A 与 D 字节相同（都是 `[escape(obj) for obj in value]`）。保留 A、B、C、E，把与 A 重复的 D 重做为 `return list(value)`（不转义）。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：escape 替 conditional_escape）
```diff
-    return [conditional_escape(obj) for obj in value]
+    return [escape(obj) for obj in value]
```
**变异语义**：用 `escape` 替 `conditional_escape`。`escape` 对已 `mark_safe` 的元素也强制转义（不检查 `__html__`），于是 `test_basic` 中 `b=[mark_safe("x&y"), mark_safe("<p>")]` 被二次转义成 `x&amp;y`/`&lt;p&gt;`，而期望原样 `x&y`/`<p>`。断言失败。保留。

### Group B — 保留（B3 条件：过滤掉 safe 元素）
```diff
-    return [conditional_escape(obj) for obj in value]
+    return [conditional_escape(obj) for obj in value if not hasattr(obj, "__html__")]
```
**变异语义**：列表推导加 `if not hasattr(obj, "__html__")` 过滤条件，把 `SafeString`（有 `__html__`）元素整个丢弃而非保留。`test_basic` 的 b 列表元素全被滤掉 → join 结果为空串而非 `x&y, <p>`，元素个数也不对。断言失败。保留。

### Group C — 保留（C1 类型：对整序列而非逐元素）
```diff
-    return [conditional_escape(obj) for obj in value]
+    return conditional_escape(value)
```
**变异语义**：`conditional_escape(value)` 对整个 list 一次性处理而非逐元素，且返回的不是 list 而是对 list 的 str 表示转义后的 `SafeString`。`escapeseq` 退化——后续 `join` 拿到的不是元素序列而是单个字符串，逐项 join 行为完全错。F2P 全部子用例失败。保留。

### Group D — 补充（D1 状态：原样拷贝不转义）
```diff
-    return [conditional_escape(obj) for obj in value]
+    return list(value)
```
**变异语义**：`return list(value)` 原样拷贝序列、完全不转义。`escapeseq` 形同恒等过滤器。autoescape off 时（`test_autoescape_off`/`test_chain_join_autoescape_off`）`x&y`/`<p>` 不被转义，输出含裸 `&` 和 `<>`，与期望的 `x&amp;y`/`&lt;p&gt;` 不符。模拟"忘了调转义、只是把序列转成 list"。比 A（用错转义函数）更彻底——根本不转义。补充为 D。

### Group E — 保留（E2 隐式→显式开关）
```diff
-def escapeseq(value):
+def escapeseq(value, force_escape_safe=False):
...
-    return [conditional_escape(obj) for obj in value]
+    if force_escape_safe:
+        return [conditional_escape(obj) for obj in value]
+    else:
+        from django.utils.html import escape
+        return [escape(obj) for obj in value]
```
**变异语义**：新增 `force_escape_safe` 参数（默认 False），默认走 `else` 分支用 `escape` 而非 `conditional_escape`，对 mark_safe 元素也强制转义。模板默认调用不传该参数 → safe 元素被破坏。模拟"把 safe 元素保护做成可配置、默认却关掉（退化成强制 escape）"。保留。

## 新设计 Mutation 说明

原始 A、D 字节完全相同（`[escape(obj) for obj in value]`）。本次保留 A（escape 替 conditional_escape）、B（过滤掉 safe 元素）、C（对整序列、返回非 list）、E（force_escape_safe 默认关闭开关），把与 A 重复的 D 重做为 `return list(value)`（原样不转义）。五组覆盖"用错转义函数 / 过滤掉 safe / 整序列处理 / 完全不转义 / 默认关闭开关"五个角度。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P（`test_escapeseq` 模块）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
