# django__django-13109

## 问题背景

`ForeignKey.validate()` 在验证外键值时使用了 `_default_manager`（默认管理器），而非 `_base_manager`（基础管理器）。当关联模型的默认管理器通过自定义 `get_queryset()` 过滤了某些对象（如 `archived=True` 的记录），那些本应合法的外键引用会被 `validate()` 错误地拒绝，抛出 `ValidationError`。

Golden patch 将 `_default_manager` 改为 `_base_manager`，使 FK 验证能够访问所有数据库记录，而不受自定义管理器过滤的影响。

## Golden Patch 语义分析

```python
# base_commit 状态（有 bug）：
qs = self.remote_field.model._default_manager.using(using).filter(
    **{self.remote_field.field_name: value}
)

# patched 状态（修复后）：
qs = self.remote_field.model._base_manager.using(using).filter(
    **{self.remote_field.field_name: value}
)
```

修复的核心语义：
- `_default_manager`：模型的默认管理器，可能被自定义（如 `WriterManager` 过滤 `archived=False`），用于应用层面的数据访问
- `_base_manager`：基础管理器，不含自定义过滤，直接访问所有数据库行，适合框架内部用于完整性检查
- FK 验证的目的是确认"该 PK 在关联表中确实存在"，不应受到业务层过滤的干扰

## 调用链分析

```
Article.full_clean()  # or form.is_valid() → _post_clean() → full_clean()
  → Article._validate_unique() / Article.clean_fields()
  → ForeignKey.validate(value=author.pk, model_instance=article)
      if value is None: return
      using = router.db_for_read(Author, instance=article)
      qs = Author._base_manager.using(using).filter(id=author.pk)
      # _base_manager = plain Manager (no custom filtering)
      # → finds all authors including archived ones
      qs = qs.complex_filter(self.get_limit_choices_to())  # applies limit_choices_to restriction
      if not qs.exists():
          raise ValidationError(...)  # only raises if PK not in DB at all, or excluded by limit_choices_to
```

F2P 测试路径：
- `author = Author.objects.create(name="Randy", archived=True)` → 创建 archived 作者
- `Author.objects` = `AuthorManager` (只返回 `archived=False`) → 但对象已存在于 DB
- `Article(author=author).full_clean()` → ForeignKey.validate(author.pk)
- 修复前：`AuthorManager.filter(id=pk)` → archived 作者被过滤 → `exists()=False` → raise ValidationError!
- 修复后：`_base_manager.filter(id=pk)` → 找到 → `exists()=True` → no error ✓

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | `_base_manager` 改回 `_default_manager`，直接还原 golden patch |
| B | 🔴 必须替换 | 替换 | `not qs.exists()` 改为 `qs.exists()`，破坏 P2P（`test_limited_FK_raises_error`） |
| C | 缺失 | 新设计 | C 组缺失，设计新 mutation |
| D | 缺失 | 新设计 | D 组缺失，设计新 mutation |
| E | 缺失 | 新设计 | E 组缺失，设计新 mutation |

原有 A 是直接逆操作，B 反转了验证逻辑（会导致所有 P2P 测试失败，包括 `test_limited_FK_raises_error`）。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：`_base_manager` → `_default_manager`（直接逆操作）

**分类**：🔴 必须替换

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/related.py b/django/db/models/fields/related.py
index c2aea9c730..b9e19b2301 100644
--- a/django/db/models/fields/related.py
+++ b/django/db/models/fields/related.py
@@ -914,7 +914,7 @@ class ForeignKey(ForeignObject):
             return
 
         using = router.db_for_read(self.remote_field.model, instance=model_instance)
-        qs = self.remote_field.model._base_manager.using(using).filter(
+        qs = self.remote_field.model._meta.default_manager.using(using).filter(
             **{self.remote_field.field_name: value}
         )
         qs = qs.complex_filter(self.get_limit_choices_to())
```

**变异语义**：将 `_base_manager` 改为通过 `_meta.default_manager` 访问默认管理器。`_meta.default_manager` 等同于 `_default_manager`，都指向带自定义过滤的管理器（如 `WriterManager`/`AuthorManager`）。对于 archived 记录：default_manager 过滤了 `archived=False` → 找不到 archived author → raise ValidationError → F2P 失败。P2P 测试（无自定义管理器的模型）：default_manager = 普通 Manager，无过滤 → 正常工作。看起来像是"使用元类接口访问"的等价重写，但实际访问了错误的管理器。

---

### Group B — 替换

**原 mutation**：`if not qs.exists():` → `if qs.exists():` raise（反转逻辑）

**分类**：🔴 必须替换（破坏 P2P：`test_limited_FK_raises_error` 会失败）

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/related.py b/django/db/models/fields/related.py
index c2aea9c730..71f709b902 100644
--- a/django/db/models/fields/related.py
+++ b/django/db/models/fields/related.py
@@ -917,6 +917,9 @@ class ForeignKey(ForeignObject):
         qs = self.remote_field.model._base_manager.using(using).filter(
             **{self.remote_field.field_name: value}
         )
+        qs = qs.filter(
+            pk__in=self.remote_field.model._default_manager.using(using).values('pk')
+        )
         qs = qs.complex_filter(self.get_limit_choices_to())
         if not qs.exists():
             raise exceptions.ValidationError(
```

**变异语义**：在 base_manager 查询结果基础上，增加一个与 default_manager 的交集过滤：`pk__in=_default_manager.values('pk')`。作用：只保留 _default_manager 也能看到的对象（即 archived=False 的对象）。对于 archived author（pk=X）：_default_manager.values('pk') 不含 X → qs 为空 → raise ValidationError → F2P 失败。P2P（无自定义管理器模型）：_default_manager 包含所有 pk → 交集无变化 → 正常工作。看起来像是"确保只验证应用层可见的对象"的安全改进，实际上重新引入了自定义管理器过滤的影响。

---

### Group C — 新设计

**原 mutation**：（缺失）

**分类**：C1（使用 managers[0] 即第一个注册的管理器）

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/related.py b/django/db/models/fields/related.py
index c2aea9c730..bd90af979f 100644
--- a/django/db/models/fields/related.py
+++ b/django/db/models/fields/related.py
@@ -914,7 +914,7 @@ class ForeignKey(ForeignObject):
             return
 
         using = router.db_for_read(self.remote_field.model, instance=model_instance)
-        qs = self.remote_field.model._base_manager.using(using).filter(
+        qs = self.remote_field.model._meta.managers[0].using(using).filter(
             **{self.remote_field.field_name: value}
         )
         qs = qs.complex_filter(self.get_limit_choices_to())
```

**变异语义**：通过 `_meta.managers[0]` 访问模型注册的第一个管理器，而非 `_base_manager`。对于 Author 模型：`objects = AuthorManager` 是第一个（也是唯一）显式注册的管理器，`_meta.managers[0]` 即 `AuthorManager`。archived author 被 AuthorManager 过滤 → not found → raise → F2P 失败。对于无自定义管理器的模型（如 ModelToValidate）：`managers[0]` 是 Django 自动创建的普通 Manager，无过滤 → P2P 正常。看起来像是"使用第一个/主要管理器进行验证"的合理想法，但忽略了第一个管理器可能有业务过滤。

---

### Group D — 新设计

**原 mutation**：（缺失）

**分类**：D3（引入条件依赖：根据 limit_choices_to 是否设置来选择管理器）

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/related.py b/django/db/models/fields/related.py
index c2aea9c730..da409f2f47 100644
--- a/django/db/models/fields/related.py
+++ b/django/db/models/fields/related.py
@@ -914,7 +914,10 @@ class ForeignKey(ForeignObject):
             return
 
         using = router.db_for_read(self.remote_field.model, instance=model_instance)
-        qs = self.remote_field.model._base_manager.using(using).filter(
+        qs = (
+            self.remote_field.model._base_manager if self.get_limit_choices_to()
+            else self.remote_field.model._default_manager
+        ).using(using).filter(
             **{self.remote_field.field_name: value}
         )
         qs = qs.complex_filter(self.get_limit_choices_to())
```

**变异语义**：根据 `get_limit_choices_to()` 是否返回非空值来选择管理器：如果有 `limit_choices_to`，使用 `_base_manager`；否则使用 `_default_manager`。逻辑是"当需要额外限制时才用 base_manager 避免双重过滤，否则用 default_manager"。对于 Article.author FK（无 limit_choices_to）：`get_limit_choices_to()` 返回空 `{}` → falsy → 使用 `_default_manager` → 过滤 archived → F2P 失败。对于 ModelToValidate.parent FK（有 limit_choices_to={'number': 10}）：使用 `_base_manager` → 找到 parent → complex_filter 剔除 number=11 的 → raise ✓。P2P 通过。难以发现：这种"条件选择"的逻辑看起来是一种微妙的优化，但倒置了正确的使用场景。

---

### Group E — 新设计

**原 mutation**：（缺失）

**分类**：E2（用显式的 `.objects` 属性代替内部 `_base_manager` 属性）

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/related.py b/django/db/models/fields/related.py
index c2aea9c730..4dcffcf066 100644
--- a/django/db/models/fields/related.py
+++ b/django/db/models/fields/related.py
@@ -914,7 +914,7 @@ class ForeignKey(ForeignObject):
             return
 
         using = router.db_for_read(self.remote_field.model, instance=model_instance)
-        qs = self.remote_field.model._base_manager.using(using).filter(
+        qs = self.remote_field.model.objects.using(using).filter(
             **{self.remote_field.field_name: value}
         )
         qs = qs.complex_filter(self.get_limit_choices_to())
```

**变异语义**：将 `_base_manager`（框架内部接口）改为 `.objects`（用户层接口，通常是默认管理器）。`.objects` 属性指向用户定义的 `objects = WriterManager()`（或 `objects = AuthorManager()`）。对于 archived 对象：`Author.objects`（AuthorManager）过滤 `archived=False` → not found → raise → F2P 失败。对于无自定义 objects 的模型：`Model.objects` = 普通 Manager → 无过滤 → P2P 正常。看起来像是"更 Pythonic 的写法"：用 `.objects` 替代内部 `_base_manager` 属性，实际上引入了用户层过滤逻辑。

---

## 新设计 Mutation 说明

所有 5 个 mutation 都使得 archived 记录（通过自定义默认管理器过滤的记录）在 FK 验证时被"找不到"，从而错误地触发 ValidationError。关键区别：

| 组 | 访问方式 | 使用的管理器（对 Author 模型） |
|---|---|---|
| A | `_meta.default_manager` | AuthorManager（filtered） |
| B | `_base_manager` + `_default_manager pk__in` 交集 | 交集 = AuthorManager 的范围 |
| C | `_meta.managers[0]` | AuthorManager（filtered） |
| D | 条件判断，无 limit_choices_to → `_default_manager` | AuthorManager（filtered） |
| E | `.objects` | AuthorManager（filtered） |

P2P 安全性：`ModelToValidate.parent` FK 对应的 `ModelToValidate` 模型无自定义管理器，以上所有变体的实际管理器都退化为普通 Manager（无过滤），`test_limited_FK_raises_error` 依赖的是 `complex_filter(limit_choices_to)` 的过滤，不受管理器选择影响。
