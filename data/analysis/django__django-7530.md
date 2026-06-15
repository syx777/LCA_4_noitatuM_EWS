# django__django-7530

## 问题背景

`makemigrations` 命令在执行历史一致性检查时，调用 `router.allow_migrate()` 传入了错误的 `(app_label, model_name)` 组合。具体表现为：对每个 `app_label`，使用了 `apps.get_models(app_label)` 来获取模型列表——但 `apps.get_models()` 的签名是 `get_models(include_auto_created=False, include_swapped=False)`，`app_label` 字符串被当作 `include_auto_created` 的位置参数（非空字符串为 truthy），导致返回的是**所有已安装 app 的全部模型**，而非该 app 专属的模型。

结果：`allow_migrate('other', 'migrations', model_name='SomeModelFromAnotherApp')` 这类非法组合被传入 router，破坏了依赖 `(app_label, model_name)` 对应关系的自定义 router（如分片数据库场景）。

## Golden Patch 语义分析

```diff
-                    for model in apps.get_models(app_label)
+                    for model in apps.get_app_config(app_label).get_models()
```

修复核心：将全局 `apps.get_models(app_label)`（参数被误用为 bool）替换为 `apps.get_app_config(app_label).get_models()`，后者明确获取指定 app 的 `AppConfig` 对象，再调用其 `get_models()` 方法，保证迭代出的每个 `model` 确实属于当前 `app_label`，从而使 `allow_migrate(alias, app_label, model_name=model._meta.object_name)` 的参数组合始终合法。

## 调用链分析

```
Command.handle()
  ├── apps.get_app_configs()         → 获取所有 app 的 config，构建 consistency_check_labels
  ├── connections / DATABASE_ROUTERS → 决定 aliases_to_check
  ├── router.allow_migrate(alias, app_label, model_name=...)
  │     └── 调用自定义 router（如 TestRouter.allow_migrate）
  │           → 依赖 (app_label, model_name) 组合的合法性
  └── loader.check_consistent_history(connection)
        → 检查该连接上已应用的迁移历史是否一致
```

数据流：`consistency_check_labels`（所有 app 标签集合）→ 外层循环 `app_label` → 内层循环 `model`（应属于该 app）→ 传入 `allow_migrate`。Bug 在于内层循环的 model 来源错误，破坏了 `app_label` 与 `model` 的对应关系。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 高质量（接口契约变异） | 保留 | 修改 consistency_check_labels 构建逻辑，影响哪些 app 被检查，语义深层 |
| B | 高质量（逻辑反转变异） | 保留 | 反转 DATABASE_ROUTERS 条件，影响哪些数据库被检查，真实开发者易犯的逻辑错误 |
| C | 高质量（参数替换变异） | 保留 | 用 model._meta.app_label 替代 app_label，在多 app 或 proxy model 场景下行为不同 |
| D | 高质量（API 参数变异） | 保留 | 传入 include_auto_created=True，引入 auto-created 中间表模型，重新制造跨 app 参数问题 |
| E | 高质量（条件语义变异） | 保留 | any → all，使 router 拒绝任一模型时跳过一致性检查，反转了检查触发逻辑 |

语义浅层共 0 个，无需替换。全部5个均为高质量 mutation，全部保留。

## 各组 Mutation 分析

### Group A — 保留

**原 mutation**：
```diff
diff --git a/django/core/management/commands/makemigrations.py b/django/core/management/commands/makemigrations.py
index e648880ef0..f28e32eeaa 100644
--- a/django/core/management/commands/makemigrations.py
+++ b/django/core/management/commands/makemigrations.py
@@ -96,7 +96,7 @@ class Command(BaseCommand):
         loader = MigrationLoader(None, ignore_no_migrations=True)
 
         # Raise an error if any migrations are applied before their dependencies.
-        consistency_check_labels = set(config.label for config in apps.get_app_configs())
+        consistency_check_labels = app_labels or set(config.label for config in apps.get_app_configs())
         # Non-default databases are only checked if database routers used.
         aliases_to_check = connections if settings.DATABASE_ROUTERS else [DEFAULT_DB_ALIAS]
         for alias in sorted(aliases_to_check):
```

**分类**：🟢 保留（高质量，接口契约变异）

**理由**：修改了 `consistency_check_labels` 的构建逻辑。当用户指定了 `app_labels`（如 `makemigrations myapp`）时，只检查指定 app 的一致性，跳过其他 app。这模拟了真实开发者的一个常见误解：认为 makemigrations 只需要检查用户指定的 app。当 `app_labels` 为空（无参数调用）时行为与原始代码相同，所以简单测试可能通过；只有在多 app 环境且指定 app 时，才会暴露问题（其他 app 的不一致历史被跳过）。

**最终 mutation**：
```diff
diff --git a/django/core/management/commands/makemigrations.py b/django/core/management/commands/makemigrations.py
index e648880ef0..f28e32eeaa 100644
--- a/django/core/management/commands/makemigrations.py
+++ b/django/core/management/commands/makemigrations.py
@@ -96,7 +96,7 @@ class Command(BaseCommand):
         loader = MigrationLoader(None, ignore_no_migrations=True)
 
         # Raise an error if any migrations are applied before their dependencies.
-        consistency_check_labels = set(config.label for config in apps.get_app_configs())
+        consistency_check_labels = app_labels or set(config.label for config in apps.get_app_configs())
         # Non-default databases are only checked if database routers used.
         aliases_to_check = connections if settings.DATABASE_ROUTERS else [DEFAULT_DB_ALIAS]
         for alias in sorted(aliases_to_check):
```

**变异语义**：当用户指定 app_labels 时，一致性检查范围被缩小为仅用户指定的 app，而非所有已安装 app。无参数调用时行为不变，因此简单测试通过；只有测试验证"指定 app 时仍检查所有 app 的历史一致性"时才失败。

---

### Group B — 保留

**原 mutation**：
```diff
diff --git a/django/core/management/commands/makemigrations.py b/django/core/management/commands/makemigrations.py
index e648880ef0..0c750d6f91 100644
--- a/django/core/management/commands/makemigrations.py
+++ b/django/core/management/commands/makemigrations.py
@@ -98,7 +98,7 @@ class Command(BaseCommand):
         # Raise an error if any migrations are applied before their dependencies.
         consistency_check_labels = set(config.label for config in apps.get_app_configs())
         # Non-default databases are only checked if database routers used.
-        aliases_to_check = connections if settings.DATABASE_ROUTERS else [DEFAULT_DB_ALIAS]
+        aliases_to_check = [DEFAULT_DB_ALIAS] if settings.DATABASE_ROUTERS else connections
         for alias in sorted(aliases_to_check):
             connection = connections[alias]
             if (connection.settings_dict['ENGINE'] != 'django.db.backends.dummy' and any(
```

**分类**：🟢 保留（高质量，逻辑反转变异）

**理由**：反转了 `DATABASE_ROUTERS` 条件的含义。原逻辑：有 router 时检查所有数据库（router 决定哪些模型迁到哪里），无 router 时只检查默认库。变异后：有 router 时只检查默认库（跳过非默认库的一致性检查），无 router 时检查所有库。这是一个典型的条件反转错误，在没有配置 `DATABASE_ROUTERS` 的简单项目中行为相同（两者都只检查默认库），只在多数据库 + router 场景下失败。

**最终 mutation**：
```diff
diff --git a/django/core/management/commands/makemigrations.py b/django/core/management/commands/makemigrations.py
index e648880ef0..0c750d6f91 100644
--- a/django/core/management/commands/makemigrations.py
+++ b/django/core/management/commands/makemigrations.py
@@ -98,7 +98,7 @@ class Command(BaseCommand):
         # Raise an error if any migrations are applied before their dependencies.
         consistency_check_labels = set(config.label for config in apps.get_app_configs())
         # Non-default databases are only checked if database routers used.
-        aliases_to_check = connections if settings.DATABASE_ROUTERS else [DEFAULT_DB_ALIAS]
+        aliases_to_check = [DEFAULT_DB_ALIAS] if settings.DATABASE_ROUTERS else connections
         for alias in sorted(aliases_to_check):
             connection = connections[alias]
             if (connection.settings_dict['ENGINE'] != 'django.db.backends.dummy' and any(
```

**变异语义**：有 DATABASE_ROUTERS 配置时反而只检查默认库，无 router 时检查所有库。单库项目测试通过；多库 + router 场景下，非默认数据库的历史不一致会被静默跳过，不抛出异常。

---

### Group C — 保留

**原 mutation**：
```diff
diff --git a/django/core/management/commands/makemigrations.py b/django/core/management/commands/makemigrations.py
index e648880ef0..f558b78692 100644
--- a/django/core/management/commands/makemigrations.py
+++ b/django/core/management/commands/makemigrations.py
@@ -103,7 +103,7 @@ class Command(BaseCommand):
             connection = connections[alias]
             if (connection.settings_dict['ENGINE'] != 'django.db.backends.dummy' and any(
                     # At least one model must be migrated to the database.
-                    router.allow_migrate(connection.alias, app_label, model_name=model._meta.object_name)
+                    router.allow_migrate(connection.alias, model._meta.app_label, model_name=model._meta.object_name)
                     for app_label in consistency_check_labels
                     for model in apps.get_app_config(app_label).get_models()
             )):
```

**分类**：🟢 保留（高质量，参数替换变异）

**理由**：将 `allow_migrate` 的第二个参数从循环变量 `app_label` 改为 `model._meta.app_label`。在 `get_app_config(app_label).get_models()` 修复后，对于普通模型两者相等，所以简单测试通过。但对于 proxy model、abstract model 的子类，或 contrib app 中 app_label 被 `AppConfig` 覆盖的情况，`model._meta.app_label` 可能与 `app_label` 不同，导致 router 收到错误的 app_label，影响路由决策。这是一个语义上很微妙的错误，代码审查难以发现。

**最终 mutation**：
```diff
diff --git a/django/core/management/commands/makemigrations.py b/django/core/management/commands/makemigrations.py
index e648880ef0..f558b78692 100644
--- a/django/core/management/commands/makemigrations.py
+++ b/django/core/management/commands/makemigrations.py
@@ -103,7 +103,7 @@ class Command(BaseCommand):
             connection = connections[alias]
             if (connection.settings_dict['ENGINE'] != 'django.db.backends.dummy' and any(
                     # At least one model must be migrated to the database.
-                    router.allow_migrate(connection.alias, app_label, model_name=model._meta.object_name)
+                    router.allow_migrate(connection.alias, model._meta.app_label, model_name=model._meta.object_name)
                     for app_label in consistency_check_labels
                     for model in apps.get_app_config(app_label).get_models()
             )):
```

**变异语义**：传给 router 的 app_label 来自 model 的元数据而非循环变量。对于自定义 AppConfig 中 label 与 model._meta.app_label 不一致的情况，router 会收到错误的 app_label。测试需要专门验证 allow_migrate 被调用时的 app_label 参数才能检测到此 bug。

---

### Group D — 保留

**原 mutation**：
```diff
diff --git a/django/core/management/commands/makemigrations.py b/django/core/management/commands/makemigrations.py
index e648880ef0..6da9f44913 100644
--- a/django/core/management/commands/makemigrations.py
+++ b/django/core/management/commands/makemigrations.py
@@ -105,7 +105,7 @@ class Command(BaseCommand):
                     # At least one model must be migrated to the database.
                     router.allow_migrate(connection.alias, app_label, model_name=model._meta.object_name)
                     for app_label in consistency_check_labels
-                    for model in apps.get_app_config(app_label).get_models()
+                    for model in apps.get_app_config(app_label).get_models(include_auto_created=True)
             )):
                 loader.check_consistent_history(connection)
 
```

**分类**：🟢 保留（高质量，API 参数变异）

**理由**：为 `get_models()` 传入 `include_auto_created=True`，使其也返回 ManyToMany 关系自动创建的中间表模型。这些 auto-created 模型的 `_meta.object_name` 是内部名称（如 `Author_books`），router 通常不认识这些 model_name，可能返回意外的迁移决策。此外，auto-created 模型的 `_meta.app_label` 可能与其所在 app 的 label 不完全一致。这个变异看起来"更完整"（包含了更多模型），但实际上引入了不必要的中间表模型，导致 router 被调用时收到它不期望的 model_name。

**最终 mutation**：
```diff
diff --git a/django/core/management/commands/makemigrations.py b/django/core/management/commands/makemigrations.py
index e648880ef0..6da9f44913 100644
--- a/django/core/management/commands/makemigrations.py
+++ b/django/core/management/commands/makemigrations.py
@@ -105,7 +105,7 @@ class Command(BaseCommand):
                     # At least one model must be migrated to the database.
                     router.allow_migrate(connection.alias, app_label, model_name=model._meta.object_name)
                     for app_label in consistency_check_labels
-                    for model in apps.get_app_config(app_label).get_models()
+                    for model in apps.get_app_config(app_label).get_models(include_auto_created=True)
             )):
                 loader.check_consistent_history(connection)
 
```

**变异语义**：allow_migrate 被额外调用 auto-created 的 M2M 中间表模型。router 会收到如 `model_name='Author_books'` 这类内部名称，导致路由决策异常。测试需要验证 allow_migrate 只被调用显式定义的模型时才能检测到此 bug。

---

### Group E — 保留

**原 mutation**：
```diff
diff --git a/django/core/management/commands/makemigrations.py b/django/core/management/commands/makemigrations.py
index e648880ef0..7847ffa88c 100644
--- a/django/core/management/commands/makemigrations.py
+++ b/django/core/management/commands/makemigrations.py
@@ -101,7 +101,7 @@ class Command(BaseCommand):
         aliases_to_check = connections if settings.DATABASE_ROUTERS else [DEFAULT_DB_ALIAS]
         for alias in sorted(aliases_to_check):
             connection = connections[alias]
-            if (connection.settings_dict['ENGINE'] != 'django.db.backends.dummy' and any(
+            if (connection.settings_dict['ENGINE'] != 'django.db.backends.dummy' and all(
                     # At least one model must be migrated to the database.
                     router.allow_migrate(connection.alias, app_label, model_name=model._meta.object_name)
                     for app_label in consistency_check_labels
```

**分类**：🟢 保留（高质量，条件语义变异）

**理由**：将 `any(...)` 改为 `all(...)`，完全反转了一致性检查的触发条件。原语义：只要有**任意一个** (app_label, model) 组合被允许迁移，就执行一致性检查。变异后：只有**所有** (app_label, model) 组合都被允许迁移，才执行一致性检查。在有 router 禁止部分模型迁移的场景（如 TestRouter 让 `db='other'` 返回 False），`all()` 为 False，跳过对 'other' 数据库的一致性检查，即使 'other' 上存在不一致的迁移历史也不会被发现。这个变异在所有模型都允许迁移的简单项目中行为相同，只在多数据库 + 部分路由场景下失败。

**最终 mutation**：
```diff
diff --git a/django/core/management/commands/makemigrations.py b/django/core/management/commands/makemigrations.py
index e648880ef0..7847ffa88c 100644
--- a/django/core/management/commands/makemigrations.py
+++ b/django/core/management/commands/makemigrations.py
@@ -101,7 +101,7 @@ class Command(BaseCommand):
         aliases_to_check = connections if settings.DATABASE_ROUTERS else [DEFAULT_DB_ALIAS]
         for alias in sorted(aliases_to_check):
             connection = connections[alias]
-            if (connection.settings_dict['ENGINE'] != 'django.db.backends.dummy' and any(
+            if (connection.settings_dict['ENGINE'] != 'django.db.backends.dummy' and all(
                     # At least one model must be migrated to the database.
                     router.allow_migrate(connection.alias, app_label, model_name=model._meta.object_name)
                     for app_label in consistency_check_labels
```

**变异语义**：一致性检查触发条件从"任意模型允许迁移"变为"所有模型都允许迁移"。当 router 禁止部分模型迁移时（常见于分片/多租户场景），all() 返回 False，跳过一致性检查，历史不一致错误被静默忽略。

## 新设计 Mutation 说明

本实例所有5个 mutation 均为新设计（实例不在 mutations.jsonl 中）：

- **Mutation A**：基于对 `consistency_check_labels` 构建逻辑的分析。`app_labels` 是用户指定的参数（可为空集合），用它替代完整的 app_configs 列表，模拟了"只检查用户关心的 app"的错误假设。
- **Mutation B**：基于对 `DATABASE_ROUTERS` 条件分支的分析。条件反转是真实开发者在阅读"有 router 时检查更多 vs 更少"时容易犯的逻辑错误。
- **Mutation C**：基于对 `allow_migrate` 参数语义的分析。`app_label`（循环变量）vs `model._meta.app_label`（模型元数据）在修复后的代码中通常相等，但在 proxy model 或自定义 AppConfig 场景下不同，是一个隐蔽的语义差异。
- **Mutation D**：基于对 `AppConfig.get_models()` API 的分析。`include_auto_created=True` 看似"更完整"，但引入了 router 不认识的内部模型名，重新制造了参数不匹配问题。
- **Mutation E**：基于对 `any/all` 语义的分析。在有 router 的场景下，`all()` 几乎总是 False（因为至少有一个模型被 router 禁止），导致一致性检查被系统性跳过。
