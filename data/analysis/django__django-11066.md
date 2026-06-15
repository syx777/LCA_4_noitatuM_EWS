# django__django-11066

## 问题背景

`RenameContentType._rename()` 在执行 content type 重命名时，使用 `content_type.save(update_fields={'model'})` 而没有传入 `using=db` 参数。在多数据库（multi-database）场景下，Django ORM 会将 `save()` 操作路由到数据库路由器决定的数据库（通常是 `default`），而非迁移所在的目标数据库（如 `other`）。这导致在非默认数据库上执行 migration 时，content type 重命名被错误地写入默认数据库，目标数据库上的 content type 未被更新。

Golden patch 将 `content_type.save(update_fields={'model'})` 修改为 `content_type.save(using=db, update_fields={'model'})`，明确指定保存到正确的数据库。

## Golden Patch 语义分析

修复的核心语义：**强制 ORM 写操作绑定到当前 schema_editor 所使用的数据库连接**。

`db = schema_editor.connection.alias` 已正确获取了目标数据库别名（例如 `'other'`）。`ContentType.objects.db_manager(db).get_by_natural_key(...)` 也正确地从目标数据库查询记录。但在 `save()` 时缺少 `using=db`，导致写操作走路由器默认路径（`db_for_write` 返回 `'default'`），从而写入了错误的数据库。

修复的关键在于：Django 的 `Model.save()` 与 `Model.objects.db_manager(db).xxx()` 是独立的——前者决定写入哪个库，后者决定读取哪个库；必须对 `save()` 也显式传递 `using=db`。

## 调用链分析

```
migrations 执行框架
  └─ inject_rename_contenttypes_operations(plan, using=db_alias)
       └─ 向 migration.operations 中插入 RenameContentType 实例
            └─ RenameContentType.rename_forward(apps, schema_editor)
                 └─ RenameContentType._rename(apps, schema_editor, old_model, new_model)
                      ├─ db = schema_editor.connection.alias  ← 正确获取目标库
                      ├─ router.allow_migrate_model(db, ContentType)  ← 检查是否允许迁移
                      ├─ ContentType.objects.db_manager(db).get_by_natural_key(...)  ← 从目标库读取
                      ├─ content_type.save(using=db, update_fields={'model'})  ← [FIXED] 写入目标库
                      └─ ContentType.objects.clear_cache()  ← 清除自然键缓存
```

`inject_rename_contenttypes_operations` 由 `post_migrate` 信号触发，在 migration plan 中每个 `RenameModel` 操作后注入一个 `RenameContentType` 操作。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原 mutation 与 C 完全相同，均为 golden patch 的直接逆操作 |
| B | 新设计 | 替换 | 原 mutations.jsonl 中只有 A/C 两条，B/D/E 需全新设计 |
| C | 🔴 必须替换 | 替换 | 原 mutation 与 A 完全相同，直接冗余 |
| D | 新设计 | 替换 | 原 mutations.jsonl 中缺失 D 组 |
| E | 新设计 | 替换 | 原 mutations.jsonl 中缺失 E 组 |

注：原始 mutations.jsonl 中仅存在 A、C 两条记录且内容完全相同（均为去除 `using=db`），均为🔴必须替换级别。B/D/E 三组需全新设计。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
diff --git a/django/contrib/contenttypes/management/__init__.py b/django/contrib/contenttypes/management/__init__.py
index 563cba2fdf..2b5f688136 100644
--- a/django/contrib/contenttypes/management/__init__.py
+++ b/django/contrib/contenttypes/management/__init__.py
@@ -24,7 +24,7 @@ class RenameContentType(migrations.RunPython):
             content_type.model = new_model
             try:
                 with transaction.atomic(using=db):
-                    content_type.save(using=db, update_fields={'model'})
+                    content_type.save(update_fields={'model'})
```

**分类**：🔴 必须替换

**理由**：这是 golden patch 的直接逆操作（从已修复状态去掉 `using=db`），完全等价于"撤销修复"。任何知道 golden patch 的测试都能直接检测到它，且与 Group C 完全相同，构成直接冗余。

**最终 mutation**（替换）：
```diff
diff --git a/django/contrib/contenttypes/management/__init__.py b/django/contrib/contenttypes/management/__init__.py
index 563cba2fdf..a9187f8d37 100644
--- a/django/contrib/contenttypes/management/__init__.py
+++ b/django/contrib/contenttypes/management/__init__.py
@@ -23,7 +23,7 @@ class RenameContentType(migrations.RunPython):
         else:
             content_type.model = new_model
             try:
-                with transaction.atomic(using=db):
+                with transaction.atomic(using=DEFAULT_DB_ALIAS):
                     content_type.save(using=db, update_fields={'model'})
             except IntegrityError:
                 # Gracefully fallback if a stale content type causes a
```

**变异语义**：`transaction.atomic()` 包裹的是默认数据库的事务，而 `content_type.save(using=db)` 写入的是目标数据库。两者操作不同的数据库连接，导致 save 操作不在任何事务保护下（对 `db` 而言是裸写）。在非默认数据库迁移时，如果 save 失败（如 IntegrityError），Django 可能无法正确回滚目标库，或者错误地认为默认库需要回滚。这个 bug 在单数据库场景下完全不可见，只在多数据库 + 事务完整性测试场景下失败。

---

### Group B — 替换（新设计）

**原 mutation**：（mutations.jsonl 中不存在 B 组，全新设计）

**分类**：新设计（高质量）

**最终 mutation**：
```diff
diff --git a/django/contrib/contenttypes/management/__init__.py b/django/contrib/contenttypes/management/__init__.py
index 563cba2fdf..fd6f1a80e6 100644
--- a/django/contrib/contenttypes/management/__init__.py
+++ b/django/contrib/contenttypes/management/__init__.py
@@ -30,10 +30,11 @@ class RenameContentType(migrations.RunPython):
                 # conflict as remove_stale_contenttypes will take care of
                 # asking the user what should be done next.
                 content_type.model = old_model
+                ContentType.objects.clear_cache()
             else:
                 # Clear the cache as the `get_by_natual_key()` call will cache
                 # the renamed ContentType instance by its old model name.
-                ContentType.objects.clear_cache()
+                pass
 
     def rename_forward(self, apps, schema_editor):
         self._rename(apps, schema_editor, self.old_model, self.new_model)
```

**变异语义**：将 `ContentType.objects.clear_cache()` 从 `else`（成功分支）移到 `except IntegrityError`（失败分支）。效果：重命名成功后缓存不清除，导致后续 `get_by_natural_key('contenttypes_tests', 'foo')` 仍命中缓存并返回已重命名的旧对象（其 model 字段已被修改为 `new_model`）。测试 `assert_foo_contenttype_not_cached`（在 `0002_rename_foo` 中）验证缓存行为，此 mutation 会使该断言失败，因为缓存中仍然存在旧名称的 ContentType 实例，但其 model 字段已不一致。简单的"重命名是否发生"测试通过，只有验证缓存一致性的测试失败。

---

### Group C — 替换

**原 mutation**：
```diff
diff --git a/django/contrib/contenttypes/management/__init__.py b/django/contrib/contenttypes/management/__init__.py
index 563cba2fdf..2b5f688136 100644
--- a/django/contrib/contenttypes/management/__init__.py
+++ b/django/contrib/contenttypes/management/__init__.py
@@ -24,7 +24,7 @@ class RenameContentType(migrations.RunPython):
             content_type.model = new_model
             try:
                 with transaction.atomic(using=db):
-                    content_type.save(using=db, update_fields={'model'})
+                    content_type.save(update_fields={'model'})
```

**分类**：🔴 必须替换（与 Group A 完全相同，直接冗余）

**理由**：与 A 组 mutation 完全相同，构成直接冗余。

**最终 mutation**（替换）：
```diff
diff --git a/django/contrib/contenttypes/management/__init__.py b/django/contrib/contenttypes/management/__init__.py
index 563cba2fdf..b99953a45f 100644
--- a/django/contrib/contenttypes/management/__init__.py
+++ b/django/contrib/contenttypes/management/__init__.py
@@ -17,7 +17,7 @@ class RenameContentType(migrations.RunPython):
             return
 
         try:
-            content_type = ContentType.objects.db_manager(db).get_by_natural_key(self.app_label, old_model)
+            content_type = ContentType.objects.get_by_natural_key(self.app_label, old_model)
         except ContentType.DoesNotExist:
             pass
         else:
```

**变异语义**：移除 `db_manager(db)`，使 `get_by_natural_key` 通过路由器查询（默认走 `default` 库）。在非默认数据库迁移时，content type 记录在 `other` 库中，`default` 库中没有，导致 `DoesNotExist` 异常，重命名被静默跳过。表面上看代码逻辑正确（异常处理完整），只有在多数据库场景下才暴露问题。单数据库下所有测试通过，多数据库测试（`test_existing_content_type_rename_other_database`）失败。

---

### Group D — 替换（新设计）

**原 mutation**：（mutations.jsonl 中不存在 D 组，全新设计）

**分类**：新设计（高质量）

**最终 mutation**：
```diff
diff --git a/django/contrib/contenttypes/management/__init__.py b/django/contrib/contenttypes/management/__init__.py
index 563cba2fdf..00a162aa3d 100644
--- a/django/contrib/contenttypes/management/__init__.py
+++ b/django/contrib/contenttypes/management/__init__.py
@@ -77,7 +77,7 @@ def inject_rename_contenttypes_operations(plan=None, apps=global_apps, using=DEF
         for index, operation in enumerate(migration.operations):
             if isinstance(operation, migrations.RenameModel):
                 operation = RenameContentType(
-                    migration.app_label, operation.old_name_lower, operation.new_name_lower
+                    migration.app_label, operation.new_name_lower, operation.old_name_lower
                 )
                 inserts.append((index + 1, operation))
         for inserted, (index, operation) in enumerate(inserts):
```

**变异语义**：在 `inject_rename_contenttypes_operations` 中构建 `RenameContentType` 时，将 `old_name_lower` 和 `new_name_lower` 对调。这导致注入的操作在"向前迁移"时实际执行的是反向重命名（尝试将 `renamedfoo` → `foo`），而在向后迁移时执行 `foo` → `renamedfoo`，与预期完全相反。`assertOperationsInjected` 仅验证结构（`assertIsInstance`、`app_label`、`old_model`、`new_model`），而 **验证时使用的是 `operation.old_name_lower`/`operation.new_name_lower`（原始 RenameModel 的字段）而不是 RenameContentType 的参数**——因此结构断言仍然通过，但实际重命名方向错误。`test_existing_content_type_rename` 会失败，因为迁移后 `foo` 仍存在，`renamedfoo` 并不存在。

---

### Group E — 替换（新设计）

**原 mutation**：（mutations.jsonl 中不存在 E 组，全新设计）

**分类**：新设计（高质量）

**最终 mutation**：
```diff
diff --git a/django/contrib/contenttypes/management/__init__.py b/django/contrib/contenttypes/management/__init__.py
index 563cba2fdf..826a2f0740 100644
--- a/django/contrib/contenttypes/management/__init__.py
+++ b/django/contrib/contenttypes/management/__init__.py
@@ -17,7 +17,7 @@ class RenameContentType(migrations.RunPython):
             return
 
         try:
-            content_type = ContentType.objects.db_manager(db).get_by_natural_key(self.app_label, old_model)
+            content_type = ContentType.objects.db_manager(DEFAULT_DB_ALIAS).get_by_natural_key(self.app_label, old_model)
         except ContentType.DoesNotExist:
             pass
         else:
```

**变异语义**：将 `db_manager(db)` 改为 `db_manager(DEFAULT_DB_ALIAS)`，使 content type 的查询强制走默认数据库，而 `save(using=db)` 仍然写入目标数据库。在非默认数据库场景：从 `default` 库查不到 content type（它在 `other` 库），抛出 `DoesNotExist`，静默跳过重命名。与 Group C 的区别在于：C 依赖路由器决定查哪个库（行为取决于路由器配置），E 显式硬编码了 `DEFAULT_DB_ALIAS`，语义更明确但同样错误。两者在单数据库场景完全通过，仅在多数据库场景下失败。

---

## 新设计 Mutation 说明

### Group A（重新设计）

基于分析：golden patch 修复了 `save()` 缺少 `using=db`，但 `transaction.atomic()` 的 `using` 参数同样关键——它决定哪个数据库连接上打开事务。将 `transaction.atomic(using=db)` 改为 `transaction.atomic(using=DEFAULT_DB_ALIAS)` 模拟了开发者"知道 save 要加 using=db，但忘记 atomic 也要同步修改"的错误。这是跨函数参数的一致性错误，非常自然。

### Group B（新设计）

缓存清除逻辑的分支错误：开发者可能误解注释的意思，认为"缓存应该在旧模型名不再存在时清除"（即 IntegrityError 时也要清），而正确逻辑是"只有成功重命名才需要清除缓存"。这类错误难以在代码审查中发现，因为注释（"Clear the cache as the get_by_natual_key() call will cache..."）并不直接说明清除时机，而且 `pass` 替换只是让 else 分支为空，语法合法。

### Group D（新设计）

模拟开发者对 `old_name_lower`/`new_name_lower` 语义的混淆：在 `RenameModel` 操作中，`old_name_lower` 是旧名称，`new_name_lower` 是新名称；而 `RenameContentType.__init__` 的签名是 `(app_label, old_model, new_model)`。开发者可能错误地认为"inject 时需要反转方向以配合 RunPython 的 forward/backward"，导致参数顺序颠倒。

### Group E（新设计）

模拟开发者"partial fix"错误：知道需要加 `using` 参数，修复了 `save()` 调用，但忘记 `get_by_natural_key` 的 `db_manager` 也已使用正确的 `db`，误将其改为显式的 `DEFAULT_DB_ALIAS`（认为"明确指定总比依赖变量更安全"）。与 Group C 形成对比：C 是忘记加 `db_manager`，E 是错误地替换了正确的 `db` 参数。
