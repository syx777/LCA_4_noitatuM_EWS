# django__django-12308

## 问题背景

Django admin 在以只读方式（readonly_fields）展示 `JSONField` 字段值时，会直接调用 `display_for_value()`，该函数对 `dict`/`list` 类型最终执行 `str(value)`，从而输出 Python repr 格式（如 `{'foo': 'bar'}`），而非合法的 JSON 字符串（如 `{"foo": "bar"}`）。此外，对于含有不可 JSON 序列化键的字典（如 tuple key），不应抛出未处理异常，而应优雅降级为 Python repr 展示。

Golden patch 在 `display_for_field()` 中为 `models.JSONField` 增加了一个专属分支：
- 当 `value` 为真值时，调用 `field.get_prep_value(value)`（即 `json.dumps(value, cls=field.encoder)`）进行 JSON 序列化；
- 若序列化失败（`TypeError`，如 tuple key），降级调用 `display_for_value(value, empty_value_display)`，最终输出 `str(value)`。

## Golden Patch 语义分析

Golden patch 的核心逻辑：

```python
elif isinstance(field, models.JSONField) and value:
    try:
        return field.get_prep_value(value)
    except TypeError:
        return display_for_value(value, empty_value_display)
```

- `field.get_prep_value(value)` 调用 `json.dumps(value, cls=self.encoder)`，正确产生 JSON 字符串；
- `and value` 守卫：当 `value` 为 falsy（但非 None，None 已在上方分支处理）时跳过此分支，走默认 `display_for_value`；
- `except TypeError`：`json.dumps` 对不可序列化的 key（如 tuple）抛出 `TypeError`，此时降级为 `str(value)` 展示，保持一致性；
- 不使用 `json.dumps(value)` 而使用 `field.get_prep_value(value)`，以尊重 `JSONField` 的 `encoder` 自定义参数。

## 调用链分析

```
admin readonly 渲染
  └─ display_for_field(value, field, empty_value_display)        [django/contrib/admin/utils.py]
       ├─ [JSONField 分支] field.get_prep_value(value)           [django/db/models/fields/json.py]
       │     └─ json.dumps(value, cls=self.encoder)
       └─ [fallback] display_for_value(value, empty_value_display) [django/contrib/admin/utils.py]
             └─ str(value) / formats.number_format / etc.
```

`display_for_value` 中 `isinstance(value, (list, tuple))` 分支返回 `', '.join(str(v) for v in value)`，对 dict 不做特殊处理，最终 `str(dict)` 输出 Python repr 格式。

## 替换决策总览

原始 mutations.jsonl 中只有 B、C、D 三条，C 和 D 完全相同，B 为简单取反。全部需要替换，同时需要补充 A 和 E。

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | （缺失）| 新设计 | mutations.jsonl 中无 A 组 |
| B | 🔴 必须替换 | 替换 | `and not value` 是 golden patch 的直接逻辑取反，明显人工痕迹 |
| C | 🔴 必须替换 | 替换 | 删除 try/except 保留单行，与 D 完全重复 |
| D | 🔴 必须替换 | 替换 | 与 C diff 完全相同，两者互为冗余 |
| E | （缺失）| 新设计 | mutations.jsonl 中无 E 组 |

语义浅层共 0 个（原有 3 条均为必须替换），新设计全部 5 个。

## 各组 Mutation 分析

### Group A — 替换（新设计）
**原 mutation**：（缺失）

**分类**：新设计（API Specifications & Contracts）

**理由**：A1 策略 — 修改函数的条件判断参数语义。将 `and value`（truthy 守卫，处理所有 falsy 非 None 值）改为 `and isinstance(value, (dict, list))`，语义变为"仅对复合容器类型做 JSON 特殊处理"，隐含假设是"字符串/数字等 JSON primitive 不需要 JSON 序列化"。实际上 `'a'` 字符串需要被序列化为 `'"a"'`（带引号的 JSON 字符串），该 mutation 会使字符串值绕过 JSON 分支，直接走 `display_for_value` → `str('a')` = `'a'`，与期望的 `'"a"'` 不同。

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/utils.py b/django/contrib/admin/utils.py
index 021a086e65..7022de8a77 100644
--- a/django/contrib/admin/utils.py
+++ b/django/contrib/admin/utils.py
@@ -398,7 +398,7 @@ def display_for_field(value, field, empty_value_display):
         return formats.number_format(value)
     elif isinstance(field, models.FileField) and value:
         return format_html('<a href="{}">{}</a>', value.url, value)
-    elif isinstance(field, models.JSONField) and value:
+    elif isinstance(field, models.JSONField) and isinstance(value, (dict, list)):
         try:
             return field.get_prep_value(value)
         except TypeError:
```

**变异语义**：开发者错误地认为 JSON primitive（字符串、数字、布尔值）不需要经过 JSON 序列化，只有容器类型（dict/list）才需要。字符串 `'a'` 期望展示为 `'"a"'`（JSON 格式），但此 mutation 使其走 `display_for_value` → `str('a')` = `'a'`。所有测试中含字符串类型的 JSONField 测试失败，但 dict/list 测试通过，难以通过简单类型测试发现。

---

### Group B — 替换（新设计）
**原 mutation**：
```diff
-    elif isinstance(field, models.JSONField) and value:
+    elif isinstance(field, models.JSONField) and not value:
```

**分类**：🔴 必须替换 — `not value` 是 `and value` 的直接逻辑取反，该分支在任何非 falsy 值时都不执行，明显不符合真实开发者行为。

**理由**：将条件取反后，只有 falsy 值（如 `{}`、`[]`、`0`）才进入 JSONField 分支，对所有实际 JSON 数据均跳过，极为不自然。

**最终 mutation**（新设计，B1 边界条件）：
```diff
diff --git a/django/contrib/admin/utils.py b/django/contrib/admin/utils.py
index 021a086e65..10f75e1004 100644
--- a/django/contrib/admin/utils.py
+++ b/django/contrib/admin/utils.py
@@ -398,7 +398,7 @@ def display_for_field(value, field, empty_value_display):
         return formats.number_format(value)
     elif isinstance(field, models.FileField) and value:
         return format_html('<a href="{}">{}</a>', value.url, value)
-    elif isinstance(field, models.JSONField) and value:
+    elif isinstance(field, models.JSONField) and value and not isinstance(value, str):
         try:
             return field.get_prep_value(value)
         except TypeError:
```

**变异语义**：开发者认为字符串类型不需要 JSON 特殊处理（"字符串本身就是文本，不需要序列化"）。对于字符串类型 `value='a'`，条件 `not isinstance(value, str)` 为 False，跳过 JSON 分支，走 `display_for_value` → `str('a')` = `'a'`，而测试期望 `'"a"'`。对于 dict/list 值，行为不变，通过简单测试。

---

### Group C — 替换（新设计）
**原 mutation**：
```diff
-        try:
-            return field.get_prep_value(value)
-        except TypeError:
-            return display_for_value(value, empty_value_display)
+        return field.get_prep_value(value)
```

**分类**：🔴 必须替换 — 与 D 完全相同，两者为重复 mutation，且删除 try/except 是直接机械操作，缺乏真实开发者语义。

**最终 mutation**（新设计，C1 Type & Data Shape）：在 `display_for_value` 中扩展 `isinstance(value, (list, tuple))` 为包含 `dict`，影响 TypeError 降级路径。

```diff
diff --git a/django/contrib/admin/utils.py b/django/contrib/admin/utils.py
index 021a086e65..c16b67abdb 100644
--- a/django/contrib/admin/utils.py
+++ b/django/contrib/admin/utils.py
@@ -422,7 +422,7 @@ def display_for_value(value, empty_value_display, boolean=False):
         return formats.localize(value)
     elif isinstance(value, (int, decimal.Decimal, float)):
         return formats.number_format(value)
-    elif isinstance(value, (list, tuple)):
+    elif isinstance(value, (list, tuple, dict)):
         return ', '.join(str(v) for v in value)
     else:
         return str(value)
```

**变异语义**：开发者在扩展 `display_for_value` 时，错误地将 `dict` 加入了 list/tuple 的格式化分支（"dict 也是可迭代的容器"）。这改变了 dict 在 `display_for_value` 中的展示方式：迭代 dict 只产生 key，`', '.join(str(k) for k in d)` 仅展示键。对于 `display_for_field(value={('a','b'):'c'}, field=JSONField())` 的 TypeError 路径，期望 `"{('a', 'b'): 'c'}"` 但实际得到 `"('a', 'b')"` — 仅为 key 的 str 形式，测试失败。对于正常 JSON 值（dict/list），它们走 `get_prep_value` 路径而非 `display_for_value`，故大多数测试通过。

---

### Group D — 替换（新设计）
**原 mutation**：
```diff
（与 C 完全相同，略）
```

**分类**：🔴 必须替换 — 与 C 完全重复。

**最终 mutation**（新设计，D — I/O & Environment，跨文件修改 `json.py`）：在 `JSONField.get_prep_value` 中为字符串值添加早期返回，跳过 JSON 编码。

```diff
diff --git a/django/db/models/fields/json.py b/django/db/models/fields/json.py
index edc5441799..a6440777c8 100644
--- a/django/db/models/fields/json.py
+++ b/django/db/models/fields/json.py
@@ -83,6 +83,8 @@ class JSONField(CheckFieldDefaultMixin, Field):
     def get_prep_value(self, value):
         if value is None:
             return value
+        if isinstance(value, str):
+            return value
         return json.dumps(value, cls=self.encoder)
```

**变异语义**：开发者认为字符串值"已经是字符串了"，直接返回而无需 JSON 编码。`get_prep_value('a')` 原本返回 `'"a"'`（JSON 表示），mutation 使其返回 `'a'`（裸字符串）。这不仅影响 admin 展示，还影响数据库写入：字符串 JSON 值存储时不会被正确序列化。F2P 测试中 `('a', '"a"')` 这一 case 将失败（得到 `'a'` 而非 `'"a"'`）。这是一个跨函数的多层 bug。

---

### Group E — 替换（新设计）
**原 mutation**：（缺失）

**分类**：新设计（E — Test-expectation Alignment）

**最终 mutation**（E1，改变异常类型，精准破坏断言期望）：
```diff
diff --git a/django/contrib/admin/utils.py b/django/contrib/admin/utils.py
index 021a086e65..a96632dc7f 100644
--- a/django/contrib/admin/utils.py
+++ b/django/contrib/admin/utils.py
@@ -401,7 +401,7 @@ def display_for_field(value, field, empty_value_display):
     elif isinstance(field, models.JSONField) and value:
         try:
             return field.get_prep_value(value)
-        except TypeError:
+        except ValueError:
             return display_for_value(value, empty_value_display)
     else:
         return display_for_value(value, empty_value_display)
```

**变异语义**：开发者将 `except TypeError` 误写为 `except ValueError`。`json.dumps({('a','b'):'c'})` 抛出的是 `TypeError: keys must be strings`，而非 `ValueError`，因此 `except ValueError` 无法捕获该异常，异常直接向上传播。F2P 测试中对 `{('a', 'b'): 'c'}` 的测试会因未处理的 `TypeError` 而失败，而其他合法 JSON 值（不触发 except 分支）全部通过。这是一个非常难以发现的 typo 级错误。

---

## 新设计 Mutation 说明

**Group A**：基于对 `display_for_field` 条件链的分析，`and value` 的作用是过滤所有 falsy 值（None 已在上游处理），而非按类型过滤。真实开发者可能误认为只有容器类型（dict/list）才需要 JSON 序列化，primitive 类型（字符串）可以直接展示。将守卫从 `and value` 改为 `and isinstance(value, (dict, list))` 模拟了这一认知错误，使字符串类型 JSONField 值走错分支。

**Group B**：类似 A，但从另一角度：开发者知道需要过滤某些类型，但加了一个负向条件 `not isinstance(value, str)`，而非直接限定 dict/list。两者在语义上略有差异——B 的 isinstance 守卫是排除法，A 是白名单法。两者都针对字符串类型 JSONField 值，但表达逻辑不同，测试失败的 case 相同。

**Group C**：修改位置在 `display_for_value` 而非 `display_for_field`，利用了 TypeError 降级路径。扩展 list/tuple 分支为 dict 模拟了开发者"dict 也是可迭代的容器"的误解，使 dict 被迭代为 key 的逗号列表而非 str(dict)。只影响进入 `display_for_value` 的 dict 值（即 TypeError fallback 路径），不影响正常 JSON 序列化路径。

**Group D**：跨文件 mutation，在 `json.py` 的 `get_prep_value` 中为字符串添加短路返回。模拟了开发者认为"字符串不需要再 JSON 编码"的错误。与 C 不同，D 的 bug 根因在更底层的 model field 层，而非 admin display 层，体现了"多点协调"特性。

**Group E**：精准的异常类型 typo，`TypeError` → `ValueError`。在代码中看起来完全合理（`ValueError` 是另一个常见的值错误异常），但 `json.dumps` 对不可序列化键抛出的正是 `TypeError`，导致 except 子句永不触发，异常直接传播。这是最难通过代码审查发现的 mutation 之一。
