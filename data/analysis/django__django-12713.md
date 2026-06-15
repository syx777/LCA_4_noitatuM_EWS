# django__django-12713

## 问题背景

Django Admin 中，`formfield_for_manytomany` 在设置 ManyToMany 字段的 widget 时没有检查调用方是否已经通过 `formfield_overrides` 或显式 kwargs 提供了自定义 widget。因此，即使用户在 `formfield_overrides` 中为 `ManyToManyField` 指定了自定义 widget（如 `CheckboxSelectMultiple`），也会被 `filter_vertical`/`autocomplete_fields`/`raw_id_fields` 等逻辑无条件覆盖。

Golden patch 的修复：在 `formfield_for_manytomany` 中，将整个 widget 设置逻辑包裹在 `if 'widget' not in kwargs:` 条件中，让调用方传入的 widget 享有最高优先级。

## Golden Patch 语义分析

```python
# 修复前（formfield_for_manytomany）
autocomplete_fields = self.get_autocomplete_fields(request)
if db_field.name in autocomplete_fields:
    kwargs['widget'] = AutocompleteSelectMultiple(...)  # 无条件覆盖
elif db_field.name in self.raw_id_fields:
    kwargs['widget'] = widgets.ManyToManyRawIdWidget(...)
elif db_field.name in [*self.filter_vertical, *self.filter_horizontal]:
    kwargs['widget'] = widgets.FilteredSelectMultiple(...)

# 修复后
if 'widget' not in kwargs:  # 只在调用方未提供 widget 时才设置
    autocomplete_fields = self.get_autocomplete_fields(request)
    if db_field.name in autocomplete_fields:
        kwargs['widget'] = AutocompleteSelectMultiple(...)
    elif ...
```

核心语义：通过 `formfield_overrides` 指定的 widget 会在 `formfield_for_dbfield`（line 148-149）被合并进 kwargs，之后传给 `formfield_for_manytomany`。修复后，这个 widget 不会被 admin 的 `filter_vertical` 等配置覆盖，实现"明确优于隐式"。

## 调用链分析

```
formfield_for_dbfield(db_field, request, **kwargs)
  ├── if db_field.__class__ in self.formfield_overrides:
  │     kwargs = {**self.formfield_overrides[ManyToManyField], **kwargs}
  │     # 此处 kwargs 获得了 formfield_overrides 中的 widget
  └── formfield_for_manytomany(db_field, request, **kwargs)
        ├── if 'widget' not in kwargs:   ← golden fix 的关键守卫
        │     if ... in autocomplete_fields: kwargs['widget'] = AutocompleteSelectMultiple(...)
        │     elif ... in raw_id_fields:    kwargs['widget'] = ManyToManyRawIdWidget(...)
        │     elif ... in filter_vertical:  kwargs['widget'] = FilteredSelectMultiple(...)
        └── db_field.formfield(**kwargs)
              └── return form_field (widget 已设置)
```

F2P 测试路径：
- `BandAdmin` 有 `filter_vertical=['members']` + `formfield_overrides={ManyToManyField: {'widget': CheckboxSelectMultiple}}`
- `formfield_for_dbfield` 合并 overrides → kwargs = `{widget: CheckboxSelectMultiple}`
- `formfield_for_manytomany`: `'widget' in kwargs` → True → 跳过 widget 设置块 → 保留 CheckboxSelectMultiple
- 测试断言 `field.widget.widget is CheckboxSelectMultiple` → PASS

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | 三处改 `not in` → `in`，修改了超出 golden patch 范围的代码，不自然 |
| B | 必须替换 | 替换 | 单处 `not in` → `in`，直接逻辑反转，明显可检测 |
| C | 必须替换 | 替换 | 直接还原 golden patch（去除 `if 'widget' not in kwargs:` 缩进），等价于逆操作 |
| D | 必须替换 | 替换 | 与 C 等价但加了注释 `# if 'widget' not in kwargs:`，人工痕迹明显 |
| E | 必须替换 | 替换（缺失） | E 组不存在于 mutations.jsonl，需新设计 |

语义浅层共 0 个。全部为必须替换。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：在 `formfield_for_choice_field`、`formfield_for_foreignkey`、`formfield_for_manytomany` 三处将 `if 'widget' not in kwargs:` 改为 `if 'widget' in kwargs:`，超出 golden patch 范围，修改了无关代码。

**分类**：🔴 必须替换

**理由**：三处同时修改极不自然；修改了 radio_field 和 FK 字段的代码，副作用范围太广，代码审查中立即可见。

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index 2099d14861..5ead82f8f8 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -263,11 +263,11 @@ class BaseModelAdmin(metaclass=forms.MediaDefiningClass):
                     self.admin_site,
                     using=db,
                 )
-            elif db_field.name in [*self.filter_vertical, *self.filter_horizontal]:
-                kwargs['widget'] = widgets.FilteredSelectMultiple(
-                    db_field.verbose_name,
-                    db_field.name in self.filter_vertical
-                )
+        if db_field.name in [*self.filter_vertical, *self.filter_horizontal]:
+            kwargs['widget'] = widgets.FilteredSelectMultiple(
+                db_field.verbose_name,
+                db_field.name in self.filter_vertical
+            )
         if 'queryset' not in kwargs:
             queryset = self.get_field_queryset(db, db_field, request)
```

**变异语义**：将 `filter_vertical/horizontal` 的 widget 设置从 `if 'widget' not in kwargs:` 块内移出，变成无条件执行（在 autocomplete 和 raw_id 检查之后）。这使得：即使 `formfield_overrides` 已提供了自定义 widget，只要字段在 `filter_vertical` 或 `filter_horizontal` 中，就仍然被 `FilteredSelectMultiple` 覆盖。autocomplete 和 raw_id 字段不受影响（它们在 guard 内），只有 filter 字段会失败。逻辑上看起来像是"对 filter 字段的特殊处理优先级调整"，不明显是 bug。

---

### Group B — 替换

**原 mutation**：`if 'widget' not in kwargs:` → `if 'widget' in kwargs:` in formfield_for_manytomany。

**分类**：🔴 必须替换

**理由**：单符号替换 `not in` → `in`，逻辑完全反转，代码审查时极易发现。

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index 2099d14861..cb786f38b8 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -249,15 +249,15 @@ class BaseModelAdmin(metaclass=forms.MediaDefiningClass):
             return None
         db = kwargs.get('using')
 
-        if 'widget' not in kwargs:
-            autocomplete_fields = self.get_autocomplete_fields(request)
-            if db_field.name in autocomplete_fields:
-                kwargs['widget'] = AutocompleteSelectMultiple(
-                    db_field.remote_field,
-                    self.admin_site,
-                    using=db,
-                )
-            elif db_field.name in self.raw_id_fields:
+        autocomplete_fields = self.get_autocomplete_fields(request)
+        if db_field.name in autocomplete_fields:
+            kwargs['widget'] = AutocompleteSelectMultiple(
+                db_field.remote_field,
+                self.admin_site,
+                using=db,
+            )
+        elif 'widget' not in kwargs:
+            if db_field.name in self.raw_id_fields:
                 kwargs['widget'] = widgets.ManyToManyRawIdWidget(
```

**变异语义**：将 `autocomplete_fields` 的检查移出 `if 'widget' not in kwargs:` 守卫，使得即使 kwargs 中已有自定义 widget，autocomplete 字段仍会被 `AutocompleteSelectMultiple` 覆盖。`raw_id_fields` 和 `filter_vertical` 字段通过保留的守卫仍然可以被覆盖。模拟了开发者认为"autocomplete 优先级应最高，不应被 formfield_overrides 覆盖"的逻辑误解。对于没有使用 autocomplete 字段的测试，行为与修复版完全相同。

---

### Group C — 替换

**原 mutation**：直接去除 `if 'widget' not in kwargs:` 缩进，将 widget 设置块恢复为无条件执行，与修复前代码等价。

**分类**：🔴 必须替换

**理由**：等同于逆操作 golden patch，是最直接的冗余。

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index 2099d14861..05bb50575a 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -249,7 +249,7 @@ class BaseModelAdmin(metaclass=forms.MediaDefiningClass):
             return None
         db = kwargs.get('using')
 
-        if 'widget' not in kwargs:
+        if 'widget_class' not in kwargs:
             autocomplete_fields = self.get_autocomplete_fields(request)
             if db_field.name in autocomplete_fields:
                 kwargs['widget'] = AutocompleteSelectMultiple(
```

**变异语义**：将守卫条件从检查 `'widget'` 键改为检查 `'widget_class'` 键。由于代码中从未向 kwargs 传入 `'widget_class'`，这个条件永远为 True，等同于无条件执行 widget 设置块——即总是覆盖调用方提供的 widget。这是一个极其隐蔽的 mutation：代码结构完全保留（`if '...' not in kwargs:` 形式），只是检查的键名从正确的 `'widget'` 变成了永远不存在的 `'widget_class'`。普通代码审查中很难发现这一单词差异。

---

### Group D — 替换

**原 mutation**：与 C 等价，但保留了注释 `# if 'widget' not in kwargs:`，人工痕迹明显。

**分类**：🔴 必须替换

**理由**：注释掉的代码 + 代码逻辑恢复为 bug 状态，不自然，代码审查中立即可见。

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index 2099d14861..2dc0c9dab0 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -146,7 +146,7 @@ class BaseModelAdmin(metaclass=forms.MediaDefiningClass):
             # formfield_overrides because **kwargs is more specific, and should
             # always win.
             if db_field.__class__ in self.formfield_overrides:
-                kwargs = {**self.formfield_overrides[db_field.__class__], **kwargs}
+                kwargs = {**{k: v for k, v in self.formfield_overrides[db_field.__class__].items() if k != 'widget'}, **kwargs}
 
             # Get the correct formfield.
             if isinstance(db_field, models.ForeignKey):
```

**变异语义**：在 `formfield_for_dbfield` 合并 `formfield_overrides` 到 kwargs 时，过滤掉 `'widget'` 键。这意味着：通过 `formfield_overrides` 指定的 widget（如测试中的 `CheckboxSelectMultiple`）永远不会被传递给 `formfield_for_manytomany`。因此，`formfield_for_manytomany` 中的守卫 `if 'widget' not in kwargs:` 始终为 True，总是用 `filter_vertical` 等 admin 默认 widget 覆盖用户配置。mutation 位于 `formfield_for_dbfield`（上游），而 golden fix 位于 `formfield_for_manytomany`（下游），体现了跨函数的错误传播。对不使用 `formfield_overrides` widget 的测试无影响。

---

### Group E — 替换（新设计）

**原 mutation**：（不存在，需新设计）

**分类**：🔴 必须替换（缺失）

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index 2099d14861..30d6918bdf 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -145,7 +145,7 @@ class BaseModelAdmin(metaclass=forms.MediaDefiningClass):
             # Make sure the passed in **kwargs override anything in
             # formfield_overrides because **kwargs is more specific, and should
             # always win.
-            if db_field.__class__ in self.formfield_overrides:
+            if db_field.__class__ in self.formfield_overrides and not (self.filter_vertical or self.filter_horizontal):
                 kwargs = {**self.formfield_overrides[db_field.__class__], **kwargs}
 
             # Get the correct formfield.
```

**变异语义**：在 `formfield_for_dbfield` 中，当 admin 配置了 `filter_vertical` 或 `filter_horizontal` 时，跳过 `formfield_overrides` 的合并。看起来像一种"优化"或"filter 字段不需要 override"的假设，但实际上导致：凡是 admin 有 filter_vertical/horizontal 配置，所有 M2M 字段的 `formfield_overrides` widget 都不会生效。测试中 `BandAdmin` 有 `filter_vertical=['members']`，所以 `formfield_overrides` 的 `CheckboxSelectMultiple` 不被传入 kwargs → `formfield_for_manytomany` 的守卫触发 → 被 `FilteredSelectMultiple` 覆盖 → 测试断言失败。只有同时配置 filter_vertical/horizontal 且使用 formfield_overrides widget 的场景才会暴露此 bug，普通场景完全正常。

## 新设计 Mutation 说明

### Group A（新设计）
分析 `formfield_for_manytomany` 的内部结构：三个 widget 分支（autocomplete、raw_id、filter_vertical）都在同一个 guard 内。最自然的"有限保护"错误是：让其中某个分支逃出 guard，看似是开发者认为该分支应有更高优先级。我们选择 filter_vertical 分支，因为它在逻辑上位于 "UI 展示偏好"层面，开发者可能认为它不应该被 formfield_overrides 覆盖。

### Group D（新设计）
对 `formfield_for_dbfield` 的调用链分析：widget 从 formfield_overrides 流向 kwargs，再流向 formfield_for_manytomany。在合并点（line 149）过滤掉 widget 键，模拟了开发者认为"widget 不应通过 overrides 传递给 formfield_for_fk/m2m，而应由那些方法自行决定"的逻辑误解。这比直接修改 formfield_for_manytomany 更隐蔽，因为错误发生在上游的通用方法中。

### Group E（新设计）
选择在 formfield_for_dbfield 的合并条件中加入 `not (filter_vertical or filter_horizontal)` 条件。这模拟了"当有显式的 filter UI 配置时，formfield_overrides 的 widget 不适用"的错误假设——听起来合理但实际上违反了 override 的语义契约。由于合并发生在 FK/M2M 的统一处理路径，此 bug 同时影响 ForeignKey 和 ManyToManyField，且只在特定配置组合下暴露。
