# django__django-11400

## 问题背景

`RelatedFieldListFilter` 和 `RelatedOnlyFieldListFilter` 在生成相关模型的下拉选项时，未能正确应用排序：

1. 当相关模型在 admin 站点注册了 `ModelAdmin`（带 `ordering`）时，虽能读取其排序，但会将其传入 `get_choices()`，而 `get_choices()` 原始实现无论 `ordering` 是否为空都调用 `.order_by(*ordering)`——当 `ordering=()` 时，会清除模型的 `Meta.ordering` 默认排序。
2. `RelatedOnlyFieldListFilter.field_choices()` 完全不传 `ordering` 参数。
3. 当相关模型没有注册 `ModelAdmin` 时，`ordering` 始终为 `()`，导致模型的 `Meta.ordering` 被清除，选项顺序不确定。

Golden patch 修复了以上三个问题：新增 `field_admin_ordering()` 方法抽取排序逻辑，修改 `get_choices()` 只在 `ordering` 非空时才 `order_by`，并修正 `RelatedOnlyFieldListFilter`。

## Golden Patch 语义分析

核心修复分三层：

1. **`filters.py` — 新增 `field_admin_ordering()` 方法**：将"获取 admin 排序"逻辑从 `field_choices()` 中提取为独立方法，便于 `RelatedOnlyFieldListFilter` 复用。当相关模型有注册的 `ModelAdmin` 时返回其 `get_ordering(request)`，否则返回 `()`（空元组，表示"不强制排序，由后续逻辑决定"）。

2. **`fields/__init__.py` 和 `reverse_related.py` — `get_choices()` 增加 `if ordering:` 守卫**：关键修复：原来 `.order_by(*())` 会清除模型的 `Meta.ordering`，现在只有当 `ordering` 非空时才调用 `order_by`，让空 ordering 情况下模型默认排序得以保留。

3. **`filters.py` — `RelatedOnlyFieldListFilter.field_choices()` 传入 `ordering`**：补全了该子类之前遗漏的排序支持。

## 调用链分析

```
RelatedFieldListFilter.__init__()
  └── self.field_choices(field, request, model_admin)  [在 filters.py 中]
        └── self.field_admin_ordering(field, request, model_admin)  [新增方法]
              └── related_admin.get_ordering(request)  [在 admin/options.py:334]
        └── field.get_choices(include_blank=False, ordering=ordering)
              ├── Field.get_choices()  [在 db/models/fields/__init__.py:809]
              │     └── rel_model._default_manager.complex_filter().order_by(*ordering)
              └── ForeignObjectRel.get_choices()  [在 db/models/fields/reverse_related.py:117]
                    └── self.related_model._default_manager.all().order_by(*ordering)

RelatedOnlyFieldListFilter.field_choices()  [继承自 RelatedFieldListFilter]
  └── self.field_admin_ordering(field, request, model_admin)
  └── field.get_choices(include_blank=False, limit_choices_to={...}, ordering=ordering)
```

数据流：`model_admin.admin_site._registry` → `related_admin` → `get_ordering(request)` → `ordering` → `get_choices(ordering=ordering)` → QuerySet 排序。

## 替换决策总览

| 组 | 原mutation类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 缺失 | 新建 | mutations.jsonl 中无 Group A，需新建 |
| B | 🔴 必须替换 | 替换 | `is None` 逻辑倒置会导致 `None.get_ordering()` 崩溃，且有明显注释 "# Removed fallback..." |
| C | 🔴 必须替换 | 替换 | 移除 `if ordering:` 守卫等价于直接还原 golden fix 在这两个文件的修改，是 patch 的逆操作 |
| D | 🔴 必须替换 | 替换 | 注释明写 "# Bug: ignore admin ordering" 和 "# Bug: always return empty ordering"，极不自然 |
| E | 🔴 必须替换 | 替换 | 使用 `self.use_ordering` 未定义属性，会立即 AttributeError |

语义浅层共 0 个，必须替换 4 个（B/C/D/E）+ 新建 1 个（A）。

## 各组 Mutation 分析

### Group A — 新建

**原 mutation**：（缺失，不存在）

**分类**：新建（路径A数据不完整，需补全Group A）

**理由**：mutations.jsonl 中只有 B/C/D/E 四组，缺少 Group A（API Specifications & Contracts）。

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/reverse_related.py b/django/db/models/fields/reverse_related.py
index 700410a086..28f1d4d07b 100644
--- a/django/db/models/fields/reverse_related.py
+++ b/django/db/models/fields/reverse_related.py
@@ -122,9 +122,7 @@ class ForeignObjectRel(FieldCacheMixin):
         Analog of django.db.models.fields.Field.get_choices(), provided
         initially for utilization by RelatedFieldListFilter.
         """
-        qs = self.related_model._default_manager.all()
-        if ordering:
-            qs = qs.order_by(*ordering)
+        qs = self.related_model._default_manager.all().order_by(*ordering)
         return (blank_choice if include_blank else []) + [
             (x.pk, str(x)) for x in qs
         ]
```

**变异语义**：去掉 `if ordering:` 守卫，直接调用 `.order_by(*ordering)`。当 `ordering=()` 时，Django queryset 的 `order_by()` 调用会清除模型的 `Meta.ordering`，导致无注册 admin 时的默认排序失效。代码看起来是"简洁的内联写法"，实际上破坏了"空 ordering 保留默认排序"的语义。通过所有不检查 Meta.ordering 的测试，只在 `test_relatedfieldlistfilter_foreignkey_default_ordering` 类场景（无注册 admin + Meta.ordering）下失败。

**新设计说明**：基于对 Django queryset `.order_by(*())` 语义的深层理解——Django 中 `.order_by()` 和 `.order_by(*())` 均会清除默认排序，这是 golden fix 中 `if ordering:` 守卫的存在原因。此 mutation 将 `reverse_related.py` 中的守卫去掉，将三行合并为一行内联，表面上是"代码简化"，实际上改变了语义。选择 `reverse_related.py` 而非 `__init__.py`，是因为前者处理 ManyToMany/ForeignKey 的反向关系 `get_choices`，修改更隐蔽。

---

### Group B — 替换

**原 mutation**：
```diff
diff --git a/django/contrib/admin/filters.py b/django/contrib/admin/filters.py
index a9e5563c6c..4845c0777f 100644
--- a/django/contrib/admin/filters.py
+++ b/django/contrib/admin/filters.py
@@ -198,9 +198,9 @@ class RelatedFieldListFilter(FieldListFilter):
         Return the model admin's ordering for related field, if provided.
         """
         related_admin = model_admin.admin_site._registry.get(field.remote_field.model)
-        if related_admin is not None:
+        if related_admin is None:
             return related_admin.get_ordering(request)
-        return ()
+        return ()  # Removed fallback to model._meta.ordering
```

**分类**：🔴 必须替换

**理由**：条件 `is None` 倒置后，当 `related_admin` 存在（即 `is not None`）时走 `return ()` 分支，而当 `related_admin is None` 时尝试 `None.get_ordering(request)` 会立即 `AttributeError`。此外注释 `# Removed fallback to model._meta.ordering` 明显是人工痕迹。这个 mutation 不是"难以发现的 bug"，而是会直接崩溃。

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/filters.py b/django/contrib/admin/filters.py
index a9e5563c6c..e6f1e22dd9 100644
--- a/django/contrib/admin/filters.py
+++ b/django/contrib/admin/filters.py
@@ -199,8 +199,8 @@ class RelatedFieldListFilter(FieldListFilter):
         """
         related_admin = model_admin.admin_site._registry.get(field.remote_field.model)
         if related_admin is not None:
-            return related_admin.get_ordering(request)
-        return ()
+            return ()
+        return related_admin.get_ordering(request) if related_admin else ()
```

**变异语义**：当相关模型有注册 admin 时，`field_admin_ordering()` 返回 `()`（空元组）而非 admin 的排序设置。`field_choices()` 随后用空 ordering 调用 `field.get_choices()`，在 `fields/__init__.py` 的 `if ordering:` 守卫处不进入 `order_by`，因此沿用模型默认排序（而非 admin 指定排序）。结果是：admin 的 `ordering = ('name',)` 被静默忽略，选项按 `Meta.ordering` 排序。这通过不检查排序的基础测试，只在 `test_relatedfieldlistfilter_foreignkey_ordering` 和 `test_relatedfieldlistfilter_foreignkey_ordering_reverse` 下失败。代码看起来像重构了返回逻辑，加了冗余的 `if related_admin else ()` 防护，实则逻辑互换。

---

### Group C — 替换

**原 mutation**：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index 1aad845470..bc333e4b51 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -826,8 +826,7 @@ class Field(RegisterLookupMixin):
         qs = rel_model._default_manager.complex_filter(limit_choices_to)
-        if ordering:
-            qs = qs.order_by(*ordering)
+        qs = qs.order_by(*ordering)
...
-        if ordering:
-            qs = qs.order_by(*ordering)
+        qs = qs.order_by(*ordering)
```

**分类**：🔴 必须替换

**理由**：这是对 golden patch 在 `__init__.py` 和 `reverse_related.py` 中修改的直接还原——原始 bug 的代码路径之一就是无条件调用 `.order_by(*ordering)`，该 mutation 等同于撤销这两个文件的修复，属于"直接冗余"。

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index 1aad845470..fc94d7f8fb 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -826,7 +826,7 @@ class Field(RegisterLookupMixin):
             else 'pk'
         )
         qs = rel_model._default_manager.complex_filter(limit_choices_to)
-        if ordering:
+        if ordering is not None:
             qs = qs.order_by(*ordering)
         return (blank_choice if include_blank else []) + [
             (choice_func(x), str(x)) for x in qs
```

**变异语义**：将 `if ordering:` 改为 `if ordering is not None:`。两者区别：空元组 `()` 是 falsy（`bool(()) == False`）但不是 `None`，因此 `if ordering is not None:` 在 `ordering=()` 时为真，触发 `order_by(*())`，清除 `Meta.ordering`。这是典型的"Python 真值测试 vs None 检查"混淆——代码审查者容易误认为两种写法等价。通过所有不依赖 Meta.ordering 回退的测试，只在测试"无 admin 注册时遵循 Model.Meta.ordering"的场景下失败。

---

### Group D — 替换

**原 mutation**：
```diff
diff --git a/django/contrib/admin/filters.py b/django/contrib/admin/filters.py
...
-        return ()
+        return () # Bug: always return empty ordering
 
     def field_choices(self, field, request, model_admin):
-        ordering = self.field_admin_ordering(field, request, model_admin)
+        ordering = ()  # Bug: ignore admin ordering
```

**分类**：🔴 必须替换

**理由**：注释明写 `# Bug: always return empty ordering` 和 `# Bug: ignore admin ordering`，极为不自然，代码审查中立即可见。

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/filters.py b/django/contrib/admin/filters.py
index a9e5563c6c..365ac3d7ee 100644
--- a/django/contrib/admin/filters.py
+++ b/django/contrib/admin/filters.py
@@ -199,7 +199,7 @@ class RelatedFieldListFilter(FieldListFilter):
         """
         related_admin = model_admin.admin_site._registry.get(field.remote_field.model)
         if related_admin is not None:
-            return related_admin.get_ordering(request)
+            return model_admin.get_ordering(request)
         return ()
 
     def field_choices(self, field, request, model_admin):
```

**变异语义**：`field_admin_ordering()` 本应返回**相关模型**的 admin 排序（`related_admin.get_ordering(request)`），但现在返回**当前模型**的 admin 排序（`model_admin.get_ordering(request)`）。这是"用错了 admin 对象"的典型错误：当 Book 的 ModelAdmin 没有 `ordering` 时，`model_admin.get_ordering()` 返回 `()`；而 Employee 的 `EmployeeAdminWithOrdering` 有 `ordering = ('name',)` 却被忽略。代码看起来完全合理——两者都是调用 `get_ordering`，只是对象不同。通过不检查相关模型排序的测试，只在 `test_relatedfieldlistfilter_foreignkey_ordering` 场景下失败。

---

### Group E — 替换

**原 mutation**：
```diff
diff --git a/django/contrib/admin/filters.py b/django/contrib/admin/filters.py
...
         if related_admin is not None:
-            return related_admin.get_ordering(request)
+            return ()
         return ()
 
     def field_choices(self, field, request, model_admin):
         ordering = self.field_admin_ordering(field, request, model_admin)
-        return field.get_choices(include_blank=False, ordering=ordering)
+        return field.get_choices(include_blank=False, ordering=ordering if self.use_ordering else ())
```

**分类**：🔴 必须替换

**理由**：`self.use_ordering` 未在 `RelatedFieldListFilter` 中定义，访问时立即抛 `AttributeError`，不是"难以发现"而是立即崩溃。

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/filters.py b/django/contrib/admin/filters.py
index a9e5563c6c..21e09001b9 100644
--- a/django/contrib/admin/filters.py
+++ b/django/contrib/admin/filters.py
@@ -425,5 +425,5 @@ FieldListFilter.register(lambda f: True, AllValuesFieldListFilter)
 class RelatedOnlyFieldListFilter(RelatedFieldListFilter):
     def field_choices(self, field, request, model_admin):
         pk_qs = model_admin.get_queryset(request).distinct().values_list('%s__pk' % self.field_path, flat=True)
-        ordering = self.field_admin_ordering(field, request, model_admin)
-        return field.get_choices(include_blank=False, limit_choices_to={'pk__in': pk_qs}, ordering=ordering)
+        self.field_admin_ordering(field, request, model_admin)
+        return field.get_choices(include_blank=False, limit_choices_to={'pk__in': pk_qs})
```

**变异语义**：`RelatedOnlyFieldListFilter.field_choices()` 调用了 `field_admin_ordering()` 但丢弃其返回值，未将 `ordering` 传给 `field.get_choices()`。`field.get_choices()` 使用默认 `ordering=()`，在 `fields/__init__.py` 的 `if ordering:` 守卫处不触发 `order_by`，沿用 `Meta.ordering`。这意味着 `RelatedOnlyFieldListFilter` 静默忽略了 admin 指定的排序，只走 Meta 默认排序路径。代码看起来像"只验证了 ordering 能被获取但未使用"，不像明显错误。通过所有测试 `RelatedFieldListFilter` 的用例，只在专门测试 `RelatedOnlyFieldListFilter` 排序的场景下失败。

## 新设计 Mutation 说明

### Group A（新建）
基于对 `ForeignObjectRel.get_choices()` 在 `reverse_related.py` 中的代码分析：golden fix 的关键点之一是将 `self.related_model._default_manager.order_by(*ordering)` 拆分为"先 `.all()` 再条件性 `.order_by()`"，以避免空 ordering 清除 Meta.ordering。将两步合并回一步内联看似是无害的代码简化（去掉临时变量 `qs`），但实际上恢复了"空 ordering 清除默认排序"的旧行为。选择只修改 `reverse_related.py` 而非 `__init__.py`，让影响范围更聚焦（只影响通过 `ForeignObjectRel.get_choices` 的路径）。

### Group B（替换）
分析 `field_admin_ordering()` 的调用逻辑：该方法有两个分支——admin 注册路径和 admin 未注册路径。将两个分支的返回值互换（admin 注册时返回 `()`，未注册时用 `get_ordering`），模拟了"写返回值时逻辑搞反"的真实错误。增加 `if related_admin else ()` 的防崩溃写法让代码看起来更严谨，实则掩盖了逻辑错误。

### Group C（替换）
基于 Python 真值测试的细微语义差异：`if x:` 和 `if x is not None:` 对空容器行为不同。`ordering=()` 是 falsy 但非 None，因此 `is not None` 检查会触发空元组的 `order_by(*())`。这模拟了开发者"从 None 检查改成真值检查"时的逆向错误，或者"以为 None 检查更安全"的误判。

### Group D（替换）
分析 `model_admin` 和 `related_admin` 的区别：前者是当前模型（如 Book）的管理类，后者是相关模型（如 Employee）的管理类。将 `related_admin.get_ordering` 改为 `model_admin.get_ordering` 模拟了"复制粘贴时用了错误的变量"这一常见错误。两者方法签名完全相同，代码审查仅靠变量名难以察觉。

### Group E（替换）
分析 `RelatedOnlyFieldListFilter.field_choices()` 的修复：golden fix 新增了 `ordering = self.field_admin_ordering(...)` 和将 `ordering` 传入 `field.get_choices()`。将赋值改为纯函数调用（丢弃返回值）模拟了"调用了方法但忘记使用返回值"的错误，这是重构时常见的遗漏。`get_choices()` 的 `ordering` 默认为 `()`，`if ordering:` 守卫使其不清除 Meta.ordering，因此行为变为"只用 Meta.ordering 排序而忽略 admin 指定排序"，不崩溃但排序错误。
