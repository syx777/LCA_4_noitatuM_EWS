# django__django-11551

## 问题背景

`ModelAdmin._check_list_display_item` 在 Django 2.2.1 中对使用 `django-positions` 库的 `PositionField` 会错误地报告 `admin.E108`（字段不可访问），导致 Django 无法启动。

根本原因：commit `47016adb` 为修复 bug #28490 引入了 `hasattr(obj.model, item)` 作为门控条件。但 `PositionField` 的 `__get__` 在以类（而非实例）调用时抛出 `AttributeError`，导致 `hasattr(obj.model, item)` 返回 False——即便该字段在 `_meta` 中注册了，也无法通过校验。

golden patch 修复重构了整个检查逻辑：直接先用 `_meta.get_field(item)` 尝试获取字段，若失败再回退到 `getattr(obj.model, item)`，两者均失败才返回 E108。彻底移除了 `hasattr` 门控。

## Golden Patch 语义分析

**修复前（buggy 2.2 state）**：
```python
elif hasattr(obj.model, item):    # 门控：hasattr 为 False 时完全跳过 _meta
    try:
        field = obj.model._meta.get_field(item)
    except FieldDoesNotExist:
        return []
    else:
        if isinstance(field, models.ManyToManyField):
            return [E109]
        return []
else:
    return [E108]   # hasattr=False → 直接 E108，不尝试 _meta
```

**修复后（golden fix）**：
```python
try:
    field = obj.model._meta.get_field(item)   # 先尝试 _meta
except FieldDoesNotExist:
    try:
        field = getattr(obj.model, item)       # 再回退到 getattr
    except AttributeError:
        return [E108]                          # 两者均失败才报错
if isinstance(field, models.ManyToManyField):
    return [E109]
return []
```

核心改变：
1. 移除 `hasattr` 门控，不再因 `__get__` 抛 `AttributeError` 而短路
2. 统一两条获取路径，ManyToManyField 检查移到两者之外，覆盖所有场景
3. 只有两条路径都失败时才返回 E108

## 调用链分析

```
ModelAdmin.check() 
  → ModelAdminChecks._check_list_display(obj)
    → [per item] ModelAdminChecks._check_list_display_item(obj, item, label)
      ├── callable(item)?
      ├── hasattr(obj, item)?   [obj = ModelAdmin instance]
      ├── obj.model._meta.get_field(item)   [Model._meta registry]
      │     └── FieldDoesNotExist → getattr(obj.model, item)
      │                              └── AttributeError → E108
      └── isinstance(field, ManyToManyField) → E109
```

数据流：
- `item`（字段名字符串）→ `_meta.get_field(item)` → `Field` 实例 → ManyToMany 检查 → 返回错误列表或空列表
- `obj.model` 是被 admin 管理的 Model 类（非实例）

关键约束：`getattr(Model, field_name)` 调用字段描述符的 `__get__(None, Model)`，而某些自定义字段（如 `PositionField`）在 `instance is None` 时故意抛 `AttributeError`（它们只能通过实例访问）。

## 替换决策总览

本实例在 mutations.jsonl 中仅有 1 行（Group E），按路径 B（新实例）处理，为 A/B/C/D/E 各设计 1 个全新 mutation。

| 组 | 策略代码 | 描述 |
|---|---|---|
| A | A1 | 在主 try 块之后插入 getattr 二次验证，PositionField 的 `__get__` 会触发 E108 |
| B | B3 | 在 ManyToManyField 检查之后、final return 之前插入 `not hasattr` 门控 |
| C | C1 | 在 try-except-else 的 else 子句中用 `getattr(..., None)` 做可访问性验证 |
| D | D1 | 在 try 块内获取字段后，若 `hasattr` 为 False 则重新抛出 FieldDoesNotExist |
| E | E2 | 将 `return []` 改为条件表达式，使隐式的"字段有效"逻辑显式依赖 hasattr |

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：无（路径 B，全新设计）

**分类**：新设计

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/checks.py b/django/contrib/admin/checks.py
index 0c32301284..ddb19ed812 100644
--- a/django/contrib/admin/checks.py
+++ b/django/contrib/admin/checks.py
@@ -738,6 +738,21 @@ class ModelAdminChecks(BaseModelAdminChecks):
                         id='admin.E108',
                     )
                 ]
+        try:
+            getattr(obj.model, item)
+        except AttributeError:
+            return [
+                checks.Error(
+                    "The value of '%s' refers to '%s', which is not a "
+                    "callable, an attribute of '%s', or an attribute or "
+                    "method on '%s.%s'." % (
+                        label, item, obj.__class__.__name__,
+                        obj.model._meta.app_label, obj.model._meta.object_name,
+                    ),
+                    obj=obj.__class__,
+                    id='admin.E108',
+                )
+            ]
         if isinstance(field, models.ManyToManyField):
             return [
                 checks.Error(
```

**变异语义**：在通过 `_meta.get_field`（或 getattr 回退）获取字段后，再用 `getattr(obj.model, item)` 做一次"类属性可达性"验证。开发者可能认为"meta 中存在 ≠ 类属性可访问"，因此加了这道额外检查。但 `PositionField.__get__(None, owner)` 会抛 AttributeError，导致误报 E108。普通字段（CharField 等）的 `getattr(Model, 'name')` 返回 `DeferredAttribute`，不抛异常，所以不受影响。难以发现原因：代码看起来像合理的"双重验证"。

---

### Group B — 替换

**原 mutation**：无（路径 B，全新设计）

**分类**：新设计

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/checks.py b/django/contrib/admin/checks.py
index 0c32301284..58be8f306c 100644
--- a/django/contrib/admin/checks.py
+++ b/django/contrib/admin/checks.py
@@ -746,6 +746,19 @@ class ModelAdminChecks(BaseModelAdminChecks):
                     id='admin.E109',
                 )
             ]
+        if not hasattr(obj.model, item):
+            return [
+                checks.Error(
+                    "The value of '%s' refers to '%s', which is not a "
+                    "callable, an attribute of '%s', or an attribute or "
+                    "method on '%s.%s'." % (
+                        label, item, obj.__class__.__name__,
+                        obj.model._meta.app_label, obj.model._meta.object_name,
+                    ),
+                    obj=obj.__class__,
+                    id='admin.E108',
+                )
+            ]
         return []

     def _check_list_display_links(self, obj):
```

**变异语义**：在 ManyToManyField 检查通过之后、`return []` 之前，插入 `not hasattr(obj.model, item)` 检查作为最终"兜底"验证。开发者可能认为："既然不是 ManyToManyField，还需要确认字段可以从类上访问到"。但 PositionField 的 `__get__` 使 `hasattr` 返回 False，触发 E108。难以发现原因：该检查位于流程最末尾，看起来像无害的"防御性校验"。

---

### Group C — 替换

**原 mutation**：无（路径 B，全新设计）

**分类**：新设计

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/checks.py b/django/contrib/admin/checks.py
index 0c32301284..53a47baf8c 100644
--- a/django/contrib/admin/checks.py
+++ b/django/contrib/admin/checks.py
@@ -738,6 +738,21 @@ class ModelAdminChecks(BaseModelAdminChecks):
                         id='admin.E108',
                     )
                 ]
+        else:
+            cls_field = getattr(obj.model, item, None)
+            if cls_field is None and not isinstance(field, models.ManyToManyField):
+                return [
+                    checks.Error(
+                        "The value of '%s' refers to '%s', which is not a "
+                        "callable, an attribute of '%s', or an attribute or "
+                        "method on '%s.%s'." % (
+                            label, item, obj.__class__.__name__,
+                            obj.model._meta.app_label, obj.model._meta.object_name,
+                        ),
+                        obj=obj.__class__,
+                        id='admin.E108',
+                    )
+                ]
         if isinstance(field, models.ManyToManyField):
             return [
                 checks.Error(
```

**变异语义**：利用 `try-except-else` 语法（`else` 在无异常时执行），在 `_meta.get_field` 成功后，用 `getattr(obj.model, item, None)` 做隐式类型转换验证。`getattr(Model, 'field', None)` 在 `__get__` 抛 AttributeError 时返回 None。条件 `cls_field is None and not isinstance(field, ManyToManyField)` 确保 ManyToManyField 不受影响（有它的正常检查路径）。对 PositionField：cls_field=None，not ManyToMany → E108。难以发现原因：`getattr(..., None)` 看起来是安全的"软访问"，None 检查看似合理。

---

### Group D — 替换

**原 mutation**：无（路径 B，全新设计）

**分类**：新设计

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/checks.py b/django/contrib/admin/checks.py
index 0c32301284..d0f084a8c5 100644
--- a/django/contrib/admin/checks.py
+++ b/django/contrib/admin/checks.py
@@ -722,6 +722,8 @@ class ModelAdminChecks(BaseModelAdminChecks):
             return []
         try:
             field = obj.model._meta.get_field(item)
+            if not hasattr(obj.model, item):
+                raise FieldDoesNotExist(item)
         except FieldDoesNotExist:
             try:
                 field = getattr(obj.model, item)
```

**变异语义**：在 `_meta.get_field` 成功获取字段后，立即检查 `hasattr(obj.model, item)`。若为 False，重新抛出 `FieldDoesNotExist`，进入回退路径。在回退路径中，`getattr(obj.model, item)` 对 PositionField 再次抛 AttributeError → E108。开发者可能认为："meta 中存在的字段如果无法通过 hasattr 访问，应该视为不存在，走回退逻辑"。这个模式（try 内 re-raise）在代码中很常见，看起来非常自然。

---

### Group E — 替换（替换原有 mutations.jsonl 中的 E 组 mutation）

**原 mutation（mutations.jsonl 中的）**：
```diff
+        elif not hasattr(obj.model, item):
+            return [checks.Error(...id='admin.E108'...)]
```

**分类**：🔴 必须替换 — 直接在 `_meta.get_field` try 块之前重新引入 `hasattr` 门控，本质上是 golden patch 所移除的 bug 的直接还原，缺乏隐蔽性。

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/checks.py b/django/contrib/admin/checks.py
index 0c32301284..9ca4d734b3 100644
--- a/django/contrib/admin/checks.py
+++ b/django/contrib/admin/checks.py
@@ -746,7 +746,18 @@ class ModelAdminChecks(BaseModelAdminChecks):
                     id='admin.E109',
                 )
             ]
-        return []
+        return [] if hasattr(obj.model, item) else [
+            checks.Error(
+                "The value of '%s' refers to '%s', which is not a "
+                "callable, an attribute of '%s', or an attribute or "
+                "method on '%s.%s'." % (
+                    label, item, obj.__class__.__name__,
+                    obj.model._meta.app_label, obj.model._meta.object_name,
+                ),
+                obj=obj.__class__,
+                id='admin.E108',
+            )
+        ]

     def _check_list_display_links(self, obj):
```

**变异语义**：将 `return []` 改为条件表达式，使"字段有效"的隐式语义显式依赖 `hasattr(obj.model, item)`。体现 E2（隐式行为显式化）策略：原来 `return []` 无条件表示"字段合法"，现在加了可访问性条件。对 PositionField：hasattr=False → return [E108]。看起来像是在 E109 检查之后"补充"了 E108 检查，不像直接还原，更隐蔽。

## 新设计 Mutation 说明

所有 5 个 mutation 均基于以下核心洞察：

**golden patch 的关键语义**：`_meta.get_field(item)` 成功不等于 `hasattr(obj.model, item)` 为真。PositionField 这类字段在类级别访问时会抛 AttributeError（descriptor 设计），但确实是合法注册的 model field。golden patch 通过移除 hasattr 门控解决了这个问题。

**所有 mutation 的共同目标**：在 `_meta.get_field` 成功后，以不同方式重新引入对 hasattr/getattr 的依赖，使 PositionField 场景触发 E108。

**各 mutation 的差异点**：
- A（A1）：在 try-except 块之后插入独立的 try-getattr 块，位置居中
- B（B3）：在最终 return 之前插入 not hasattr 检查，位置靠后  
- C（C1）：使用 try-except-else 的 else 子句 + `getattr(..., None)` 隐式类型转换，位置居中但机制不同
- D（D1）：在 try 块内部重新抛出 FieldDoesNotExist，使错误通过异常传播方式扩散
- E（E2）：替换最终 return 语句为条件表达式，最简洁但同样有效
