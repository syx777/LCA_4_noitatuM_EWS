# django__django-13568

## 问题背景

Django 的 `check_user_model` 系统检查中，当用户模型的 `USERNAME_FIELD` 没有设置 `unique=True` 时，会触发 `auth.E003` 错误。然而，有时开发者会通过在 `Meta.constraints` 中定义 `UniqueConstraint` 来实现字段唯一性，而不是在字段上设置 `unique=True`（原因：在 PostgreSQL 上，`unique=True` 会为 `CharField`/`TextField` 创建额外的隐式 `_like` 索引）。

Golden patch 修复：将原来的单一条件 `if not field.unique:` 扩展为复合条件：仅当字段没有 `unique=True` **且**没有覆盖该字段的 total UniqueConstraint 时，才发出错误。

## Golden Patch 语义分析

```python
# 原代码
if not cls._meta.get_field(cls.USERNAME_FIELD).unique:

# 修复后
if not cls._meta.get_field(cls.USERNAME_FIELD).unique and not any(
    constraint.fields == (cls.USERNAME_FIELD,)
    for constraint in cls._meta.total_unique_constraints
):
```

关键设计决策：
1. **`_meta.total_unique_constraints`**：只包含无条件的 `UniqueConstraint`（即没有 `condition=` 参数的约束）。带条件的约束（partial index）不能保证全字段唯一性，不应豁免检查。
2. **`constraint.fields == (cls.USERNAME_FIELD,)`**：检查约束覆盖的字段集合恰好是 `(USERNAME_FIELD,)` 单元组，不允许多字段约束满足条件。
3. **`not any(...)`**：仅当没有满足条件的约束时才触发错误，即存在任意一个完整覆盖 USERNAME_FIELD 的无条件唯一约束就豁免。

## 调用链分析

```
Django 系统检查框架 -> check_user_model(app_configs)
    ├─ cls = AUTH_USER_MODEL 对应的 model class
    ├─ cls._meta.get_field(cls.USERNAME_FIELD).unique    [字段级别 unique]
    ├─ cls._meta.total_unique_constraints               [模型级别无条件唯一约束列表]
    │       (UniqueConstraint without condition=)
    │       对比: cls._meta.constraints = 全部约束（含 partial）
    │             cls._meta.indexes = Index 对象列表（非唯一约束）
    └─ constraint.fields                                [约束覆盖的字段名元组]
            constraint.name                             [约束名称字符串]
```

数据流：
- `total_unique_constraints` 是 `_meta` 的属性，过滤掉带 `condition` 的 `UniqueConstraint`
- `constraint.fields` 是字段名的元组（如 `('username',)`）
- 整个 `any(...)` 检查是新增逻辑，替代了旧的单一 `.unique` 检查

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 新设计 | 设计新 mutation | mutations.jsonl 中仅有 E 组，需补全 A/B/C/D |
| B | 新设计 | 设计新 mutation | 同上 |
| C | 新设计 | 设计新 mutation | 同上 |
| D | 新设计 | 设计新 mutation | 同上 |
| E | 🟡 语义浅层（保留） | 保留 | `==` 改 `!=` 在关键逻辑节点，位置重要，效果直接，予以保留 |

语义浅层共 1 个（E），保留（位于核心判断逻辑，1个不需替换）。新设计 4 个（A/B/C/D）。

## 各组 Mutation 分析

### Group A — 新设计
**分类**：新设计（A2 类型：使用错误的 API 属性访问更广范围的数据）
**设计思路**：将 `cls._meta.total_unique_constraints` 替换为 `cls._meta.constraints`。后者包含**所有**约束，包括带 `condition=` 的 partial UniqueConstraint。这导致 partial unique constraint 也能豁免 E003 检查，但 partial constraint 不能保证完整唯一性。
**最终 mutation**：
```diff
diff --git a/django/contrib/auth/checks.py b/django/contrib/auth/checks.py
index c08ed8a49a..5adcf41ba5 100644
--- a/django/contrib/auth/checks.py
+++ b/django/contrib/auth/checks.py
@@ -54,7 +54,7 @@ def check_user_model(app_configs=None, **kwargs):
     # Check that the username field is unique
     if not cls._meta.get_field(cls.USERNAME_FIELD).unique and not any(
         constraint.fields == (cls.USERNAME_FIELD,)
-        for constraint in cls._meta.total_unique_constraints
+        for constraint in cls._meta.constraints
     ):
         if (settings.AUTHENTICATION_BACKENDS ==
                 ['django.contrib.auth.backends.ModelBackend']):
```
**变异语义**：partial UniqueConstraint（如带 `condition=Q(password__isnull=False)` 的约束）也能抑制 E003 错误，但这类约束不能保证 username 全局唯一。`test_username_partially_unique` 期望得到 E003 错误，但 mutation 使其被豁免 → 测试失败。

---

### Group B — 新设计
**分类**：新设计（B1 类型：逻辑运算符错误——AND 改 OR）
**设计思路**：将复合条件中的 `and` 改为 `or`。原条件：`not unique AND not any_total_constraint`（两个条件都满足才报错）。变异后：`not unique OR not any_total_constraint`（任一条件满足即报错）。对于有 `total UniqueConstraint` 但没有 `unique=True` 的模型：`not unique=True` → `not False=True` → OR 短路 → 报错，尽管存在 UniqueConstraint。
**最终 mutation**：
```diff
diff --git a/django/contrib/auth/checks.py b/django/contrib/auth/checks.py
index c08ed8a49a..43cf23135b 100644
--- a/django/contrib/auth/checks.py
+++ b/django/contrib/auth/checks.py
@@ -52,7 +52,7 @@ def check_user_model(app_configs=None, **kwargs):
         )
 
     # Check that the username field is unique
-    if not cls._meta.get_field(cls.USERNAME_FIELD).unique and not any(
+    if not cls._meta.get_field(cls.USERNAME_FIELD).unique or not any(
         constraint.fields == (cls.USERNAME_FIELD,)
         for constraint in cls._meta.total_unique_constraints
     ):
```
**变异语义**：OR 语义使得即使 total UniqueConstraint 存在，只要字段没有 `unique=True` 就会报错。`test_username_unique_with_model_constraint` 期望不报错，但 mutation 使其报错 → 测试失败。此 mutation 模拟开发者在理解"两个条件都需要满足才豁免"时，误用 OR 逻辑（"任一条件满足就需豁免"）。

---

### Group C — 新设计
**分类**：新设计（C3 类型：使用 constraint 的错误属性）
**设计思路**：将 `constraint.fields` 替换为 `constraint.name`。`fields` 是字段名元组（如 `('username',)`），`name` 是约束的名称字符串（如 `'username_unique'`）。字符串永远不等于元组 `(cls.USERNAME_FIELD,)`，所以 `any()` 始终返回 False，约束检查永远不会豁免 E003。
**最终 mutation**：
```diff
diff --git a/django/contrib/auth/checks.py b/django/contrib/auth/checks.py
index c08ed8a49a..59ca9ac54d 100644
--- a/django/contrib/auth/checks.py
+++ b/django/contrib/auth/checks.py
@@ -53,7 +53,7 @@ def check_user_model(app_configs=None, **kwargs):
 
     # Check that the username field is unique
     if not cls._meta.get_field(cls.USERNAME_FIELD).unique and not any(
-        constraint.fields == (cls.USERNAME_FIELD,)
+        constraint.name == (cls.USERNAME_FIELD,)
         for constraint in cls._meta.total_unique_constraints
     ):
         if (settings.AUTHENTICATION_BACKENDS ==
```
**变异语义**：`constraint.name` 是字符串，`(cls.USERNAME_FIELD,)` 是元组，两者类型不同，比较永远为 False。任何 total UniqueConstraint 都无法豁免检查，`test_username_unique_with_model_constraint` 期望不报错 → 实际报 E003 → 测试失败。外观上改动极小（fields → name），很难在代码审查中发现。

---

### Group D — 新设计
**分类**：新设计（D2 类型：使用语义相近但不等价的 API）
**设计思路**：将 `cls._meta.total_unique_constraints` 替换为 `cls._meta.indexes`。`indexes` 返回的是 `Index` 对象列表（普通索引，用 `indexes = [...]` 在 Meta 中定义），而非 `UniqueConstraint` 对象。`UniqueConstraint` 定义在 `constraints` 中，不在 `indexes` 中。因此，对于只有 `UniqueConstraint` 的模型，`indexes` 为空，`any()` 返回 False，无法豁免检查。
**最终 mutation**：
```diff
diff --git a/django/contrib/auth/checks.py b/django/contrib/auth/checks.py
index c08ed8a49a..d9af7fdadd 100644
--- a/django/contrib/auth/checks.py
+++ b/django/contrib/auth/checks.py
@@ -54,7 +54,7 @@ def check_user_model(app_configs=None, **kwargs):
     # Check that the username field is unique
     if not cls._meta.get_field(cls.USERNAME_FIELD).unique and not any(
         constraint.fields == (cls.USERNAME_FIELD,)
-        for constraint in cls._meta.total_unique_constraints
+        for constraint in cls._meta.indexes
     ):
         if (settings.AUTHENTICATION_BACKENDS ==
                 ['django.contrib.auth.backends.ModelBackend']):
```
**变异语义**：`_meta.indexes` 是 `Meta.indexes` 中定义的 `Index` 对象，与 `Meta.constraints` 中的 `UniqueConstraint` 是两个不同的集合。使用 indexes 代替 total_unique_constraints，使所有通过 `constraints` 定义的唯一约束都无法被识别。`test_username_unique_with_model_constraint` 使用 `UniqueConstraint` → indexes 为空 → any() False → 报 E003 → 测试失败。此 mutation 模拟开发者混淆 Django Meta 中 `constraints` 和 `indexes` 的区别。

---

### Group E — 保留
**原 mutation**：
```diff
-        constraint.fields == (cls.USERNAME_FIELD,)
+        constraint.fields != (cls.USERNAME_FIELD,)
```
**分类**：🟡 语义浅层（保留）
**理由**：虽然是单符号替换（`==` → `!=`），但位于整个新增逻辑的核心判断处。`!=` 使 `any()` 对每个覆盖 username 的约束返回 False（而不是 True），翻转了检测逻辑。模拟了开发者在双重否定逻辑（`not any(... == ...)` ↔ `all(... != ...)`）中出现混淆，认为应该"找一个不匹配的约束来证明需要额外检查"。`test_username_unique_with_model_constraint` 期望不报错 → 实际报 E003 → 测试失败。
**最终 mutation**（与原相同）：
```diff
diff --git a/django/contrib/auth/checks.py b/django/contrib/auth/checks.py
index c08ed8a49a..dc9d1c7dde 100644
--- a/django/contrib/auth/checks.py
+++ b/django/contrib/auth/checks.py
@@ -53,7 +53,7 @@ def check_user_model(app_configs=None, **kwargs):
 
     # Check that the username field is unique
     if not cls._meta.get_field(cls.USERNAME_FIELD).unique and not any(
-        constraint.fields == (cls.USERNAME_FIELD,)
+        constraint.fields != (cls.USERNAME_FIELD,)
         for constraint in cls._meta.total_unique_constraints
     ):
         if (settings.AUTHENTICATION_BACKENDS ==
```

## 新设计 Mutation 说明

### Group A — 使用 `_meta.constraints` 代替 `_meta.total_unique_constraints`
`_meta.total_unique_constraints` 是专门为"完整唯一约束"场景设计的 API，过滤掉了 partial constraints。一个不了解这个 API 的开发者可能会用更通用的 `_meta.constraints`（包含所有约束），认为"只要有唯一约束就够了"，不区分全部约束和部分约束的语义差异。

### Group B — AND 改 OR
在理解复合条件逻辑时，开发者可能混淆"两个条件都需要为真才报错"（AND）与"任何一个条件为真就报错"（OR）。OR 使得 UniqueConstraint 无法豁免字段级 unique=True 的要求，回到了比 base 代码更严格的检查（甚至更严）。

### Group C — `constraint.fields` 改 `constraint.name`
`UniqueConstraint` 对象同时有 `fields`（字段元组）和 `name`（约束名字符串）两个属性。开发者可能不确定应该比较哪个属性，误用 `name` 做字段匹配，导致类型不匹配（str vs tuple）永远返回 False。

### Group D — `_meta.total_unique_constraints` 改 `_meta.indexes`
Django Meta 中的 `constraints` 和 `indexes` 是两个独立的配置，分别对应 `UniqueConstraint`/`CheckConstraint` 和 `Index` 对象。开发者可能混淆这两个 API，认为"唯一索引"和"唯一约束"是同一回事，误将 `indexes` 用于约束检查。
