# django__django-11433

## 问题背景

此 issue 要求允许 `cleaned_data` 中的值覆盖模型字段的默认值。原有逻辑是：当字段未出现在 POST 数据中（`value_omitted_from_data` 返回 True）且字段有默认值时，`construct_instance` 会跳过该字段，直接使用模型的默认值，完全忽略 `clean()` 方法中可能对 `cleaned_data` 的修改。这导致开发者无法通过 `clean()` 来设置那些未包含在表单提交中的字段值。

Golden patch 在原有条件基础上追加了第三个条件：`and cleaned_data.get(f.name) in form[f.name].field.empty_values`。这确保只有当 `cleaned_data` 中的值也是"空值"时，才真正跳过并使用默认值；若 `clean()` 设置了非空值，则应被正常保存到实例。

## Golden Patch 语义分析

修复的核心逻辑：`construct_instance` 中跳过字段的条件从"字段有默认值 AND 值在 POST 中缺失"，改为"字段有默认值 AND 值在 POST 中缺失 AND cleaned_data 中的值是空值"。

修复的本质区分了两种情形：
1. 字段不在 POST 中，且 `clean()` 没有修改该字段（`cleaned_data` 值为空） → 应使用默认值（跳过）
2. 字段不在 POST 中，但 `clean()` 显式将 `cleaned_data[field]` 设为非空值 → 应使用 `cleaned_data` 中的值（不跳过）

## 调用链分析

```
BaseModelForm.save(commit=False)
  └── [访问 self.errors 触发 full_clean()]
        └── BaseForm.full_clean()
              ├── _clean_fields()
              ├── _clean_form()  <- 调用 clean()，用户可在此修改 cleaned_data
              └── _post_clean()
                    └── construct_instance(self, self.instance, opts.fields, opts.exclude)
                          └── 逐字段: f.save_form_data(instance, cleaned_data[f.name])
```

**数据流**：
- `form.data`（原始 POST 数据）→ 每个字段的 `to_python()` 验证 → `form.cleaned_data`
- 用户的 `clean()` 方法可修改 `cleaned_data` 中任意字段的值
- `construct_instance` 读取 `form.cleaned_data`，将值写入 model instance
- 关键：`value_omitted_from_data` 检查的是原始 POST 数据（`form.data`），而不是 `cleaned_data`

**被修改函数**：`construct_instance`（`django/forms/models.py` 第 31 行）
**直接调用者**：`BaseModelForm._post_clean`（第 396 行）
**间接调用者**：`BaseModelForm.save` → `BaseForm.full_clean` → `_post_clean`

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 直接删除 empty_values 条件，是 golden patch 的精确逆操作 |
| B | 缺失 | 新建 | mutations.jsonl 中无 B 组，需从头设计 |
| C | 🟡 语义浅层 | 保留 | `in → not in` 单符号替换，但位于核心逻辑节点，语义独特 |
| D | 🔴 必须替换 | 替换 | 与 A 等价（删除 empty_values 条件）且含 "# BUG:" 注释，极不自然 |
| E | 缺失 | 新建 | mutations.jsonl 中无 E 组，需从头设计 |

语义浅层共 1 个（C 组），floor(1/2) = 0 个替换，全部保留。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：
```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index 3ad8cea9b6..43808c8128 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -50,8 +50,7 @@ def construct_instance(form, instance, fields=None, exclude=None):
         # checkbox inputs because they don't appear in POST data if not checked.
         if (
             f.has_default() and
-            form[f.name].field.widget.value_omitted_from_data(form.data, form.files, form.add_prefix(f.name)) and
-            cleaned_data.get(f.name) in form[f.name].field.empty_values
+            form[f.name].field.widget.value_omitted_from_data(form.data, form.files, form.add_prefix(f.name))
         ):
             continue
```
**分类**：🔴 必须替换
**理由**：精确还原 base_commit 的原始 bug——删除了 golden patch 新增的 `empty_values` 检查，是 patch 的直接逆操作。

**最终 mutation**：
```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index 3ad8cea9b6..ba22fcccfc 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -51,7 +51,7 @@ def construct_instance(form, instance, fields=None, exclude=None):
         if (
             f.has_default() and
             form[f.name].field.widget.value_omitted_from_data(form.data, form.files, form.add_prefix(f.name)) and
-            cleaned_data.get(f.name) in form[f.name].field.empty_values
+            form.data.get(form.add_prefix(f.name)) in form[f.name].field.empty_values
         ):
             continue
```
**变异语义**：将 `empty_values` 判空检查的数据源从 `cleaned_data`（处理后的清洁数据）换成 `form.data`（原始 POST 数据）。当 `form.data` 中没有该字段（`form.data.get(...)` 返回 `None`），`None in empty_values` 为 True，导致即使 `clean()` 设置了非空值也会被跳过，直接使用默认值。模拟了开发者混淆"用哪个数据源判断空值"的典型错误；能通过所有 P2P 测试（只检查正常提交），仅在 F2P 测试（`clean()` 设置非空 `cleaned_data` 但 POST 为空）时失败。

---

### Group B — 新建
**分类**：🆕 新建（原 mutations.jsonl 中缺失 B 组）

**最终 mutation**（B3 — 替换 `empty_values` 检查为 `changed_data` 检查）：
```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index 3ad8cea9b6..68b43ca221 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -51,7 +51,7 @@ def construct_instance(form, instance, fields=None, exclude=None):
         if (
             f.has_default() and
             form[f.name].field.widget.value_omitted_from_data(form.data, form.files, form.add_prefix(f.name)) and
-            cleaned_data.get(f.name) in form[f.name].field.empty_values
+            f.name not in form.changed_data
         ):
             continue
```
**变异语义**：用 `f.name not in form.changed_data` 替代 `cleaned_data.get(f.name) in form[f.name].field.empty_values`。`form.changed_data` 基于原始 POST 数据与初始值的比较（不考虑 `clean()` 的修改），当字段未出现在 POST 中时，`f.name` 不在 `changed_data` 中，条件为 True → 跳过。这使得即使 `clean()` 设置了非空 `cleaned_data`，也会被跳过（使用默认值）。代码审查者容易认为"未变化的字段用默认值"是合理逻辑，但忽略了 `clean()` 的语义。F2P 测试中 `mocked_mode='de'` 时字段未在 POST 中 → 被跳过 → 失败 ✓；P2P 测试提交 `{'mode': ''}` 时 `mode in changed_data` → 不跳过 → 通过 ✓。

---

### Group C — 保留
**原 mutation**：
```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index 3ad8cea9b6..a33260152a 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -51,7 +51,7 @@ def construct_instance(form, instance, fields=None, exclude=None):
         if (
             f.has_default() and
             form[f.name].field.widget.value_omitted_from_data(form.data, form.files, form.add_prefix(f.name)) and
-            cleaned_data.get(f.name) in form[f.name].field.empty_values
+            cleaned_data.get(f.name) not in form[f.name].field.empty_values
         ):
             continue
```
**分类**：🟡 语义浅层（保留）
**理由**：`in → not in` 单符号替换，但处于 golden patch 核心逻辑的关键节点。条件取反使得"非空 cleaned_data 时跳过（使用默认值），空 cleaned_data 时不跳过"，行为与预期完全相反。在同组5个 mutation 中保留此条，因其效果与其他 mutation 逻辑不同（直接反转），且位于核心逻辑节点。

**最终 mutation**（与原相同，保留）：
```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index 3ad8cea9b6..a33260152a 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -51,7 +51,7 @@ def construct_instance(form, instance, fields=None, exclude=None):
         if (
             f.has_default() and
             form[f.name].field.widget.value_omitted_from_data(form.data, form.files, form.add_prefix(f.name)) and
-            cleaned_data.get(f.name) in form[f.name].field.empty_values
+            cleaned_data.get(f.name) not in form[f.name].field.empty_values
         ):
             continue
```
**变异语义**：反转 empty_values 检查，使字段有非空 cleaned_data 时反而跳过，空 cleaned_data 时反而不跳过。F2P 测试第二部分（空值应使用默认值）会失败：空 cleaned_data → 不跳过 → 空值覆盖默认值。

---

### Group D — 替换
**原 mutation**：
```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index 3ad8cea9b6..6027d227c8 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -50,10 +50,9 @@ def construct_instance(form, instance, fields=None, exclude=None):
         if (
             f.has_default() and
-            form[f.name].field.widget.value_omitted_from_data(form.data, form.files, form.add_prefix(f.name)) and
-            cleaned_data.get(f.name) in form[f.name].field.empty_values
+            form[f.name].field.widget.value_omitted_from_data(form.data, form.files, form.add_prefix(f.name))
         ):
-            continue
+            continue  # BUG: removed check for empty_values, breaks cleaned_data override
```
**分类**：🔴 必须替换
**理由**：与 A 组等价（删除 empty_values 条件），且含显式 "# BUG:" 注释，极不自然，代码审查中会立即被发现。

**最终 mutation**（D1 — 使用 BoundField.value() 替代 cleaned_data.get()）：
```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index 3ad8cea9b6..ada092c0ed 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -51,7 +51,7 @@ def construct_instance(form, instance, fields=None, exclude=None):
         if (
             f.has_default() and
             form[f.name].field.widget.value_omitted_from_data(form.data, form.files, form.add_prefix(f.name)) and
-            cleaned_data.get(f.name) in form[f.name].field.empty_values
+            form[f.name].value() in form[f.name].field.empty_values
         ):
             continue
```
**变异语义**：`form[f.name].value()` 返回 BoundField 的"展示值"（基于原始 POST 数据和初始值，不反映 `clean()` 的修改）。对于未出现在 POST 中的字段，`form[f.name].value()` 始终返回 `None`（而非 `clean()` 修改后的值），`None in empty_values` = True，导致 `clean()` 设置的非空值被忽略。D1 类型：使用了"状态初始化/渲染时"的值而非"清洗后"的值，模拟开发者混淆 BoundField.value() 与 cleaned_data 语义的错误。

---

### Group E — 新建
**分类**：🆕 新建（原 mutations.jsonl 中缺失 E 组）

**最终 mutation**（E2 — 将隐式行为转为依赖新参数的显式参数）：
```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index 3ad8cea9b6..8cae54cc92 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -28,7 +28,7 @@ __all__ = (
 ALL_FIELDS = '__all__'
 
 
-def construct_instance(form, instance, fields=None, exclude=None):
+def construct_instance(form, instance, fields=None, exclude=None, check_cleaned_data=False):
     """
     Construct and return a model instance from the bound ``form``'s
     ``cleaned_data``, but do not save the returned instance to the database.
@@ -51,7 +51,7 @@ def construct_instance(form, instance, fields=None, exclude=None, check_cleaned_d
         if (
             f.has_default() and
             form[f.name].field.widget.value_omitted_from_data(form.data, form.files, form.add_prefix(f.name)) and
-            cleaned_data.get(f.name) in form[f.name].field.empty_values
+            (not check_cleaned_data or cleaned_data.get(f.name) in form[f.name].field.empty_values)
         ):
             continue
```
**变异语义**：将 golden patch 新增的"检查 cleaned_data 是否为空"行为包裹在一个默认为 `False` 的参数 `check_cleaned_data` 后面。由于所有现有调用者（`_post_clean`）均使用默认参数，`not False = True`，`True or anything = True`，始终执行跳过（pre-fix 行为）。E2 类型：将一个本应默认开启的隐式行为变成需要显式传参才能激活的功能，而默认参数错误地禁用了新行为。代码审查者可能会认为"这是为了向后兼容而设计的开关"，但实际上导致修复完全失效。

---

## 新设计 Mutation 说明

### Group A（替换设计）

**代码分析基础**：`construct_instance` 有两个与字段值相关的数据源：`form.data`（原始 POST 数据）和 `form.cleaned_data`（经过 `clean()` 处理的数据）。Golden patch 的关键在于用 `cleaned_data`（而非原始 POST 数据）来判断值是否为空。

**选择位置**：保持在相同的条件语句中，只改变数据源从 `cleaned_data` 到 `form.data`。

**模拟的真实错误**：开发者在思考"字段值是否为空"时，混淆了使用清洗后的 `cleaned_data` 还是原始的 `form.data`，是一个常见的逻辑误解。

### Group B（新建设计）

**代码分析基础**：Django 表单有 `changed_data` 属性，列出所有与初始值不同的字段。对于空 POST 的字段，`changed_data` 中不会包含该字段。这是一个看起来"合理"但语义不同的检查。

**选择位置**：同一个条件语句，替换 `empty_values` 检查为 `changed_data` 检查。

**模拟的真实错误**：开发者可能认为"未变化的字段应使用默认值"，使用 `changed_data` 是一个看似合理但实际上忽略了 `clean()` 修改的错误。

### Group D（替换设计）

**代码分析基础**：`form[f.name]` 是 BoundField，`.value()` 方法返回用于渲染 HTML 表单的值（基于 POST 数据和初始值），不包含 `clean()` 的修改。`cleaned_data.get(f.name)` 则包含清洗后的值。

**选择位置**：同一条件语句，替换 `cleaned_data.get(f.name)` 为 `form[f.name].value()`。

**模拟的真实错误**：开发者混淆了 BoundField API——误用 `.value()`（展示层 API）代替直接访问 `cleaned_data`（数据层），是一个自然的接口混用错误。

### Group E（新建设计）

**代码分析基础**：`construct_instance` 是一个公开 API，可以接受额外参数。golden patch 的新行为对所有调用者都是透明的（隐式激活）。E2 策略是将隐式行为变为依赖参数的显式行为。

**选择位置**：函数签名 + 条件语句。

**模拟的真实错误**：开发者为了"向后兼容"而添加功能开关，但默认值设错（应该默认 `True` 开启新行为，却设为 `False`），导致新功能完全不生效。这是 API 演进中的典型错误。
