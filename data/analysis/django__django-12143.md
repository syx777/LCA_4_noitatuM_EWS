# django__django-12143

## 问题背景

管理后台的 `changelist_view` 中，当 `list_editable` 提交表单时，通过 `_get_edited_object_pks` 函数使用正则表达式从 POST 数据中提取被编辑对象的主键列表。原始代码直接将 `prefix` 字符串插入正则表达式，未做任何转义：

```python
pk_pattern = re.compile(r'{}-\d+-{}$'.format(prefix, self.model._meta.pk.name))
```

当 `prefix` 包含正则表达式特殊字符（如 `$`）时，`$` 被解释为行尾锚点而非字面量，导致 `pk_pattern.match(key)` 对所有实际 POST key 返回 `None`，从而 `_get_edited_object_pks` 返回空列表，`_get_list_editable_queryset` 返回空集合，最终 FormSet 处理零个对象——用户提交的修改被静默丢弃（数据丢失）。

Golden patch 修复：对 `prefix` 应用 `re.escape()`，使其中的特殊字符被转义为字面量。

## Golden Patch 语义分析

核心修复在 `_get_edited_object_pks` 方法中：

```python
# 修复前
pk_pattern = re.compile(r'{}-\d+-{}$'.format(prefix, self.model._meta.pk.name))

# 修复后
pk_pattern = re.compile(
    r'{}-\d+-{}$'.format(re.escape(prefix), self.model._meta.pk.name)
)
```

`re.escape(prefix)` 将 `prefix` 中所有非字母数字字符转义为字面量（如 `$` → `\$`），使生成的正则模式能正确匹配含特殊字符的 prefix 所生成的 POST key。注意：`pk.name` 未被转义（字段名通常是合法标识符，无需转义）。

## 调用链分析

```
changelist_view(request)
  └── FormSet = self.get_changelist_formset(request)
  └── modified_objects = self._get_list_editable_queryset(
          request, FormSet.get_default_prefix())
        └── object_pks = self._get_edited_object_pks(request, prefix)
              └── pk_pattern = re.compile(...)  ← 修复点
              └── return [value for key, value in request.POST.items()
                          if pk_pattern.match(key)]
        └── validates each pk via pk.to_python()
        └── return queryset.filter(pk__in=object_pks)
  └── formset = FormSet(request.POST, ..., queryset=modified_objects)
  └── formset.is_valid() → save changes
```

数据流：POST key（如 `form$-0-uuid`）→ 正则匹配 → PK 值列表 → ORM 过滤 → FormSet queryset → 保存变更。任何一步匹配失败都会导致 `modified_objects` 为空集，FormSet 处理零个对象。

**F2P 测试**：`test_get_list_editable_queryset_with_regex_chars_in_prefix`
- 直接调用 `_get_list_editable_queryset(request, prefix='form$')`，期望 `queryset.count() == 1`

**P2P 测试**：
- `test_get_edited_object_ids`：调用 `_get_edited_object_pks(request, prefix='form')`
- `test_get_list_editable_queryset`：调用 `_get_list_editable_queryset(request, prefix='form')`
- `test_changelist_view_list_editable_changed_objects_uses_filter`：通过 changelist_view 整体测试

## 替换决策总览

mutations.jsonl 中仅存在 1 条记录（Group D），其余4组（A/B/C/E）缺失，需全部新设计。已有的 Group D 为直接还原 golden patch，属于必须替换。

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 新设计 | 设计新 mutation | 不存在，需从零设计 |
| B | 新设计 | 设计新 mutation | 不存在，需从零设计 |
| C | 新设计 | 设计新 mutation | 不存在，需从零设计 |
| D | 🔴 必须替换 | 替换 | 直接还原 golden patch（移除 re.escape(prefix)），是 golden fix 的逆操作 |
| E | 新设计 | 设计新 mutation | 不存在，需从零设计 |

## 各组 Mutation 分析

### Group A — 新设计（替换错误目标）

**原 mutation**：无（新设计）

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index 85896bed7e..cff11f8fcd 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -1632,7 +1632,7 @@ class ModelAdmin(BaseModelAdmin):
     def _get_edited_object_pks(self, request, prefix):
         """Return POST data values of list_editable primary keys."""
         pk_pattern = re.compile(
-            r'{}-\d+-{}$'.format(re.escape(prefix), self.model._meta.pk.name)
+            r'{}-\d+-{}$'.format(prefix, re.escape(self.model._meta.pk.name))
         )
         return [value for key, value in request.POST.items() if pk_pattern.match(key)]
```

**策略码**：A1（Alter Parameter Default or Semantics）

**变异语义**：开发者将 `re.escape()` 应用于 `pk.name`（字段名，如 `uuid`）而非 `prefix`（可能含特殊字符）。逻辑上看似合理——字段名是外部标识符，可能含特殊字符，但实际上字段名是 Python 合法标识符，不会有特殊字符，而 prefix 才是真正危险的。

- **P2P 通过**：`prefix='form'` 无特殊字符，`re.escape('form')` 等于 `'form'`，未转义 prefix 无影响，`form-\d+-uuid$` 正常匹配
- **F2P 失败**：`prefix='form$'` 未转义，`$` 被解释为行尾锚，`form$-\d+-uuid$` 无法匹配 `form$-0-uuid`，count=0≠1

---

### Group B — 新设计（双重转义）

**原 mutation**：无（新设计）

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index 85896bed7e..868bd11e2c 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -1632,7 +1632,7 @@ class ModelAdmin(BaseModelAdmin):
     def _get_edited_object_pks(self, request, prefix):
         """Return POST data values of list_editable primary keys."""
         pk_pattern = re.compile(
-            r'{}-\d+-{}$'.format(re.escape(prefix), self.model._meta.pk.name)
+            r'{}-\d+-{}$'.format(re.escape(re.escape(prefix)), self.model._meta.pk.name)
         )
         return [value for key, value in request.POST.items() if pk_pattern.match(key)]
```

**策略码**：C1（Break Implicit Type Coercion）

**变异语义**：对 prefix 进行两次 `re.escape()`。第一次转义得到 `form\$`（将 `$` 转义为 `\$`），第二次又将 `\` 和 `$` 各自转义，得到 `form\\$`，在正则中变为匹配字面量 `form\$`（含反斜杠）。实际 POST key 是 `form$-0-uuid`（不含反斜杠），无法匹配。

- **P2P 通过**：`re.escape(re.escape('form'))` = `re.escape('form')` = `'form'`（纯字母无变化），pattern 正常
- **F2P 失败**：`re.escape(re.escape('form$'))` 生成 `'form\\\\$'`，正则匹配含反斜杠的 `form\$`，而 POST key 为 `form$`，不含反斜杠，count=0≠1

---

### Group C — 新设计（选择性取消 $ 转义）

**原 mutation**：无（新设计）

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index 85896bed7e..383bc02b51 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -1632,7 +1632,7 @@ class ModelAdmin(BaseModelAdmin):
     def _get_edited_object_pks(self, request, prefix):
         """Return POST data values of list_editable primary keys."""
         pk_pattern = re.compile(
-            r'{}-\d+-{}$'.format(re.escape(prefix), self.model._meta.pk.name)
+            r'{}-\d+-{}$'.format(re.escape(prefix).replace('\\$', '$'), self.model._meta.pk.name)
         )
         return [value for key, value in request.POST.items() if pk_pattern.match(key)]
```

**策略码**：B2（Remove Null/Empty Case Handling，此处引申为移除特殊字符的处理）

**变异语义**：先对 prefix 做 `re.escape()`，再将结果中的 `\$`（转义后的 `$`）还原为字面 `$`。开发者可能认为 `$` 在 HTML 表单前缀中是合法字符，不应被转义，但这使 `$` 重新成为正则元字符，恢复了原始 bug。

- **P2P 通过**：`re.escape('form')` = `'form'`，不含 `\$`，`.replace()` 无效，pattern 正常
- **F2P 失败**：`re.escape('form$')` = `'form\\$'`，`.replace('\\$', '$')` → `'form$'`，`$` 再次成为行尾锚，pattern 无法匹配 `form$-0-uuid`，count=0≠1

---

### Group D — 替换（不同函数，前置 sanitization）

**原 mutation**（直接还原 golden patch，🔴 必须替换）：
```diff
-            r'{}-\d+-{}$'.format(re.escape(prefix), self.model._meta.pk.name)
+            r'{}-\d+-{}$'.format(prefix, self.model._meta.pk.name)
```

**最终 mutation**（新设计，在 `_get_list_editable_queryset` 中 strip 特殊字符）：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index 85896bed7e..c738b58c41 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -1641,7 +1641,7 @@ class ModelAdmin(BaseModelAdmin):
         Based on POST data, return a queryset of the objects that were edited
         via list_editable.
         """
-        object_pks = self._get_edited_object_pks(request, prefix)
+        object_pks = self._get_edited_object_pks(request, re.sub(r'[^a-zA-Z0-9_\-]', '', prefix))
         queryset = self.get_queryset(request)
         validate = queryset.model._meta.pk.to_python
         try:
```

**策略码**：D4（Break Environment or Resource Handling）

**变异语义**：在 `_get_list_editable_queryset` 中，将 prefix 传给 `_get_edited_object_pks` 之前先 strip 掉所有非字母数字/下划线/破折号字符。这与 golden patch 的修改位置不同（不同函数），模拟了开发者"sanitize 输入而非转义"的错误思路。strip 操作将 `form$` 变为 `form`，导致正则模式匹配 `form-\d+-uuid$`，但 POST key 是 `form$-0-uuid`，无法匹配。

- **P2P 通过**：`re.sub(r'[^a-zA-Z0-9_\-]', '', 'form')` = `'form'`，无变化，pattern 正常
- **F2P 失败**：`re.sub(r'[^a-zA-Z0-9_\-]', '', 'form$')` = `'form'`，传入的 prefix 是 `form`，pattern `form-\d+-uuid$` 无法匹配 `form$-0-uuid`，count=0≠1

---

### Group E — 新设计（错误条件的按需转义）

**原 mutation**：无（新设计）

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index 85896bed7e..df53ea5c6a 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -1631,8 +1631,9 @@ class ModelAdmin(BaseModelAdmin):
 
     def _get_edited_object_pks(self, request, prefix):
         """Return POST data values of list_editable primary keys."""
+        escaped_prefix = re.escape(prefix) if re.search(r'\s', prefix) else prefix
         pk_pattern = re.compile(
-            r'{}-\d+-{}$'.format(re.escape(prefix), self.model._meta.pk.name)
+            r'{}-\d+-{}$'.format(escaped_prefix, self.model._meta.pk.name)
         )
         return [value for key, value in request.POST.items() if pk_pattern.match(key)]
```

**策略码**：B2（Remove Null/Empty Case Handling，此处为移除对非空白特殊字符的处理）

**变异语义**：仅在 prefix 包含空白字符时才应用 `re.escape()`，否则直接使用原始 prefix。开发者的直觉可能是"只有含空格的前缀才需要转义"，但 `$`、`^`、`(` 等非空白字符同样是正则元字符。对于 `form$` 这类常见但无空格的特殊前缀，转义不会生效。

- **P2P 通过**：`prefix='form'` 无空白，使用原始 `'form'`，与 `re.escape('form')` 等价，pattern 正常
- **F2P 失败**：`prefix='form$'` 无空白，使用原始 `'form$'`，`$` 成为正则行尾锚，pattern 无法匹配 `form$-0-uuid`，count=0≠1

---

## 新设计 Mutation 说明

### 代码分析基础

核心修复在 `_get_edited_object_pks`（L1632-1635）：将 `prefix` 用 `re.escape()` 转义后拼入正则模式。围绕这一点设计了5种不同的错误变体：

1. **Group A**（A1）：转义了错误的参数（pk.name 而非 prefix）。真实错误：开发者误判哪个参数更"危险"，pk.name 是字段名看似外部输入，但实际上是 Python 合法标识符；而 prefix 才是用户可配置的且可能含特殊字符。

2. **Group B**（C1）：对 prefix 应用两次 `re.escape()`。真实错误：开发者过度防御，认为"两次转义更安全"，但双重转义会使转义字符本身（反斜杠）也被转义，生成错误模式。

3. **Group C**（B2）：先转义再还原 `$`。真实错误：开发者认为 `$` 是合法 formset prefix 字符（如 Django 的 `CONTENT_TYPE_POSTFIX` 等），主动撤销对 `$` 的转义。这模拟了开发者对特定字符的白名单假设。

4. **Group D**（D4）：在上游函数 `_get_list_editable_queryset` 中 strip 特殊字符而非转义。真实错误：开发者选择"净化输入"策略，改变了 prefix 语义（strip 后的 prefix 与原 prefix 不一致，导致正则匹配的 key 名与实际 POST key 名不符）。修改位置在不同函数（与 golden patch 错开），模拟了真实中"修错了地方"的场景。

5. **Group E**（B2）：仅在 prefix 含空白时才转义。真实错误：开发者认为只有空白字符需要转义（类似 URL 编码的直觉），忽略了 `$`、`^`、`(` 等非空白正则元字符。
