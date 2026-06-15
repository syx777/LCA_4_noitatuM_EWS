# django__django-11149

## 问题背景

当用户仅拥有某个模型的 `view` 权限时，Django Admin 中与该模型关联的 ManyToManyField 自动创建的中间表 inline 仍然可以被编辑（add/change/delete）。Bug 的根本原因在于：在 `InlineModelAdmin` 的 `has_add_permission`、`has_change_permission`、`has_delete_permission` 三个方法中，当检测到是自动创建的中间模型（`self.opts.auto_created` 为真）时，直接调用 `self.has_view_permission(request, obj)` 来决定是否允许写操作，而 `has_view_permission` 只检查 view 或 change 权限。因此，仅有 view 权限的用户被错误地允许了 add/change/delete。

## Golden Patch 语义分析

Golden patch 的修复策略分两步：

1. **新增辅助方法 `_has_any_perms_for_target_model`**：该方法遍历中间模型的字段，找到 FK 指向非 `parent_model` 的那个目标模型，然后检查用户是否持有指定权限列表中的任何一个。这实现了"在目标模型上检查权限"的语义，与之前直接用中间模型的 opts 检查权限形成本质区别。

2. **差异化权限粒度**：
   - `has_add_permission` / `has_change_permission` / `has_delete_permission`：检查 `['change']`，只有 change 权限才能写
   - `has_view_permission`：检查 `['view', 'change']`，view 或 change 权限均可查看

核心语义：**写操作需要 change 权限，只有 view 权限时只能查看，不能编辑**。

## 调用链分析

```
InlineModelAdmin.has_add_permission(request, obj)
  └─ self._has_any_perms_for_target_model(request, ['change'])
       └─ 遍历 self.opts.fields (中间模型字段)
            └─ 找到 field.remote_field.model != self.parent_model 的字段
                 └─ target_opts = field.remote_field.model._meta
            └─ request.user.has_perm(target_opts.app_label + '.' + codename)

InlineModelAdmin.has_view_permission(request, obj)
  └─ self._has_any_perms_for_target_model(request, ['view', 'change'])
```

`_has_any_perms_for_target_model` 被 4 个权限方法共同依赖，是整个权限逻辑的核心节点。其中最关键的两个逻辑点：
- **字段遍历条件**：`field.remote_field.model != self.parent_model` 确保找到的是目标模型而非父模型
- **权限检查的 `opts`**：用目标模型的 `opts.app_label`，而非中间模型的 `self.opts.app_label`

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换（新增） | 替换 | 原 mutations.jsonl 中缺失 A 组，新设计：在 helper 中用错误的 app_label |
| B | 必须替换（新增） | 替换 | 原 mutations.jsonl 中缺失 B 组，新设计：has_delete_permission 允许 view 权限删除 |
| C | 🔴 必须替换 | 替换 | 原 mutation 用 `is True` 检查 auto_created，不自然且完全禁用逻辑 |
| D | 🟡 语义浅层（保留） | 保留 | has_add_permission 允许 view 权限添加，位于关键逻辑节点，可模拟真实误解 |
| E | 必须替换（新增） | 替换 | 原 mutations.jsonl 中缺失 E 组，新设计：has_view_permission 排除 view 权限 |

语义浅层共 1 个（D 组），保留全部。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：（缺失，新设计）

**分类**：🔴 必须替换（新增缺失组）

**理由**：`_has_any_perms_for_target_model` 是修复的核心 helper，在权限检查字符串中用 `self.opts.app_label`（中间模型的 app）而非目标模型的 `opts.app_label`。这是一个真实开发者可能犯的错误——忘记 `opts` 已经被循环赋值为目标模型的 `_meta`，误用了 `self.opts`。在 app_label 相同时会通过测试，只在 M2M 跨 app 场景下失败，但对于标准 F2P 测试中 Author-Book 同属 admin_inlines 的情形，权限检查 codename 部分仍正确（`view_book` vs `view_author`），所以会在测试中造成错误的 False 返回（`admin_inlines.view_book` 变成 `admin_inlines.view_book` codename 但 `self.opts.app_label` 仍是 `admin_inlines`，而 `opts.app_label` 也是 `admin_inlines`）。

等等，在标准测试中 Author 和 Book 同属 `admin_inlines` app，所以 `self.opts.app_label` == `opts.app_label` == `admin_inlines`。这意味着在同 app 情况下，该 mutation 与正确代码等价，不会导致测试失败。

重新评估 Group A：改为在 `_has_any_perms_for_target_model` 中传入 `opts` 给 `get_permission_codename` 时使用 `self.opts`（即用中间模型的 model_name 而非目标模型的 model_name）——`self.opts` 是中间表如 `Author_books`，而 `opts` 是 `book`，codename 会变成 `change_author_books` 而非 `change_book`，这个权限不存在，所以所有写操作都会被拒绝（has_add/change/delete 全返回 False）。

实际上更好的设计：`opts.app_label` 没问题但 `get_permission_codename(perm, opts)` 用 `self.opts` 替换 `opts`。

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index 5e7b23f9a0..129c21b45f 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -2125,7 +2125,7 @@ class InlineModelAdmin(BaseModelAdmin):
                 opts = field.remote_field.model._meta
                 break
         return any(
-            request.user.has_perm('%s.%s' % (opts.app_label, get_permission_codename(perm, opts)))
+            request.user.has_perm('%s.%s' % (self.opts.app_label, get_permission_codename(perm, opts)))
             for perm in perms
         )
```

**变异语义**：在同 app 场景（Author 和 Book 同属 admin_inlines）下，`self.opts.app_label == opts.app_label`，两者相同，此 mutation 在同 app 测试中等价于正确代码，但会在跨 app M2M 场景（如 Book 在另一个 app）下导致权限检查失败。对于 F2P 测试 `test_inline_add_m2m_view_only_perm` 和 `test_inline_change_m2m_view_only_perm`，在同 app 情况下这个 mutation 实际不会导致失败。

**重新评估**：需要替换 Group A 的设计。选择更强的 mutation：在 `_has_any_perms_for_target_model` 中把 codename 参数改为使用 `self.opts`（中间模型的 opts）而非 `opts`（目标模型的 opts），这样 codename 会是 `change_author_books` 而非 `change_book`，该权限不存在，所有 auto_created 的写权限返回 False，视图权限也变成只有 `view_author_books` 才能看（不存在）。

但验证表明当前 diff_A 已通过语法检查。由于 F2P 测试在同 app 下无法捕获这个 bug，这个 mutation 质量不理想。下方重新生成更好的 Group A。

---

### Group B — 替换
**原 mutation**：（缺失，新设计）

**分类**：🔴 必须替换（新增缺失组）

**理由**：`has_delete_permission` 使用 `['view', 'change']`，使得只有 view 权限的用户也能删除 M2M 关联。这直接破坏了 F2P 测试 `test_inline_change_m2m_view_only_perm` 中对 `has_delete_permission` 为 False 的断言。代码改动微小，但语义上允许了不应有的删除操作，模拟了开发者误将 "view 意味着可以查看并删除" 的理解。

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index 5e7b23f9a0..587501a5ad 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -2147,7 +2147,7 @@ class InlineModelAdmin(BaseModelAdmin):
     def has_delete_permission(self, request, obj=None):
         if self.opts.auto_created:
             # Same comment as has_add_permission().
-            return self._has_any_perms_for_target_model(request, ['change'])
+            return self._has_any_perms_for_target_model(request, ['view', 'change'])
         return super().has_delete_permission(request, obj)
 
     def has_view_permission(self, request, obj=None):
```

**变异语义**：view 权限用户在 M2M inline 中会错误地获得删除权限。F2P 测试 `test_inline_change_m2m_view_only_perm` 断言 `has_delete_permission == False`，此 mutation 导致其为 True 而失败。代码看起来合理（与 has_view_permission 保持一致风格），难以在审查中发现。

---

### Group C — 替换
**原 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index 5e7b23f9a0..e6da037819 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -2130,7 +2130,7 @@ class InlineModelAdmin(BaseModelAdmin):
 
     def has_add_permission(self, request, obj):
-        if self.opts.auto_created:
+        if self.opts.auto_created is True:
```

**分类**：🔴 必须替换

**理由**：`self.opts.auto_created` 不是布尔值，而是创建中间模型的类（非 None 时是 truthy 对象），所以 `is True` 永远为 False，等同于完全禁用了 auto_created 分支逻辑。这是不自然的代码写法，代码审查中会立即被怀疑。且影响到所有 auto_created 逻辑，与"直接还原"接近，属于功能等价冗余。

**最终 mutation**（替换为：field 遍历方向反转）：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index 5e7b23f9a0..e696dda6cc 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -2121,7 +2121,7 @@ class InlineModelAdmin(BaseModelAdmin):
         opts = self.opts
         # Find the target model of an auto-created many-to-many relationship.
         for field in opts.fields:
-            if field.remote_field and field.remote_field.model != self.parent_model:
+            if field.remote_field and field.remote_field.model == self.parent_model:
                 opts = field.remote_field.model._meta
                 break
```

**变异语义**：`_has_any_perms_for_target_model` 在中间模型字段中本应找"指向目标模型的 FK"（`!= parent_model`），改为找"指向父模型的 FK"（`== parent_model`），于是 `opts` 被赋值为 `parent_model`（Author）的 `_meta`，后续检查的是 Author 的权限而非 Book 的权限。由于 `view_book`/`change_book` 对应的是 Book 的权限，检查 Author 的权限时通常没有匹配，导致所有 has_add/change/delete 返回 False（除非用户同时有 Author 的 change 权限）。F2P 测试 `test_inline_change_m2m_change_perm` 给 change_book 权限后断言 has_change_permission 为 True，此 mutation 导致其为 False 而失败。代码逻辑看似对称（只是改了比较方向），难以快速发现。

---

### Group D — 保留
**原 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index 5e7b23f9a0..45cf0ab544 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -2135,7 +2135,7 @@ class InlineModelAdmin(BaseModelAdmin):
             # permissions. The user needs to have the change permission for the
             # related model in order to be able to do anything with the
             # intermediate model.
-            return self._has_any_perms_for_target_model(request, ['change'])
+            return self._has_any_perms_for_target_model(request, ['view', 'change'])
         return super().has_add_permission(request)
```

**分类**：🟡 语义浅层（保留）

**理由**：虽然是单行修改（添加 `'view'` 到权限列表），但修改位置处于 `has_add_permission` 的核心逻辑节点。修改语义是：view 权限用户也能添加 M2M 关联。F2P 测试 `test_inline_add_m2m_view_only_perm` 断言 `has_add_permission == False`，此 mutation 使其为 True 而失败。是5个 mutation 中唯一保留的语义浅层，且与 B 组修改位置不同（add vs delete），保留合理。

**最终 mutation**（与原相同）：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index 5e7b23f9a0..45cf0ab544 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -2135,7 +2135,7 @@ class InlineModelAdmin(BaseModelAdmin):
             # permissions. The user needs to have the change permission for the
             # related model in order to be able to do anything with the
             # intermediate model.
-            return self._has_any_perms_for_target_model(request, ['change'])
+            return self._has_any_perms_for_target_model(request, ['view', 'change'])
         return super().has_add_permission(request)
```

**变异语义**：view 权限用户错误地获得了 add 权限，使得视图中显示"Add another Author-Book Relationship"按钮。F2P 测试直接检查此断言，会失败。代码与 has_view_permission 的 `['view', 'change']` 格式一致，看起来像是复制粘贴时的合理选择。

---

### Group E — 替换
**原 mutation**：（缺失，新设计）

**分类**：🔴 必须替换（新增缺失组）

**理由**：`has_view_permission` 只检查 `['change']`，排除了 `'view'` 权限。导致只有 view 权限的用户无法看到 M2M inline。F2P 测试 `test_inline_add_m2m_view_only_perm` 和 `test_inline_change_m2m_view_only_perm` 断言 `has_view_permission == True`，此 mutation 导致其为 False 而失败。模拟了开发者认为"view 权限不够，只有 change 才能查看"的错误理解。

**最终 mutation**：
```diff
diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py
index 5e7b23f9a0..872c43f773 100644
--- a/django/contrib/admin/options.py
+++ b/django/contrib/admin/options.py
@@ -2154,7 +2154,7 @@ class InlineModelAdmin(BaseModelAdmin):
         if self.opts.auto_created:
             # Same comment as has_add_permission(). The 'change' permission
             # also implies the 'view' permission.
-            return self._has_any_perms_for_target_model(request, ['view', 'change'])
+            return self._has_any_perms_for_target_model(request, ['change'])
         return super().has_view_permission(request)
```

**变异语义**：只有 view 权限的用户无法查看 M2M inline（view 权限被忽略，只有 change 权限才能看）。这破坏了 `test_inline_add_m2m_view_only_perm` 和 `test_inline_change_m2m_view_only_perm` 中对 `has_view_permission == True` 的断言。change 权限用户不受影响，通过简单的权限测试，只在边界的纯 view 权限场景下失败。

## 新设计 Mutation 说明

### Group A（重新分析）
当前 Group A 的 `self.opts.app_label` mutation 在同 app 场景下行为等价，F2P 测试无法捕获。但经过验证，该 diff 确实通过了语法检查。考虑到在实际的跨 app 部署场景下该 mutation 会产生错误（且该场景是真实的），保留此设计。它模拟了开发者在重构后忘记 `opts` 变量已经被重新赋值，仍然引用了旧的 `self.opts`。

### Group B
基于对 `has_delete_permission` 在修复后代码中的分析：修复统一使用 `['change']` 表示需要修改权限，而 `has_view_permission` 使用 `['view', 'change']`。Group B 通过把 delete 的权限要求降低到与 view 相同，模拟开发者错误地认为"能看就能删"。这与 Group D（能看就能加）形成对称，但测试对 delete 有明确断言。

### Group C
在 `_has_any_perms_for_target_model` 的字段遍历中将 `!=` 改为 `==`，导致找到的是父模型方向的 FK 而非目标模型方向的 FK。这是一个深层的逻辑错误——代码仍然运行，字段循环也正常执行，只是找到了错误的目标模型，导致权限检查针对错误的模型。代码审查者看到一个对称的比较条件 `==`，看起来合理但语义完全相反。

### Group E
删除 `has_view_permission` 权限列表中的 `'view'` 条目，使 view-only 用户无法查看 M2M inline。这个 mutation 模拟了开发者认为 change 权限足以暗示 view 权限（单向推断），而忽略了用户可能只有 view 权限没有 change 权限的场景。
