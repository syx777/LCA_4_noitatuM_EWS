# django__django-13741

## 问题背景

`django.contrib.auth.forms` 中的 `ReadOnlyPasswordHashField` 用于在 `UserChangeForm` 中以只读方式展示密码哈希。原来的实现通过两个机制防止密码被意外修改：

1. `bound_data()` 始终返回 `initial`（忽略提交的数据），因为 widget 不渲染 input 字段
2. `has_changed()` 始终返回 `False`（字段永不"改变"）
3. `UserChangeForm.clean_password()` 无论提交什么都返回 `self.initial.get('password')`

Django 的 `forms.Field` 基类早已有 `disabled` 参数，当 `disabled=True` 时，基类 `bound_data` 自动返回 `initial`，`has_changed` 自动返回 `False`，并且 `_clean_fields` 直接使用 `get_initial_for_field` 而非提交数据。这三个功能与 `ReadOnlyPasswordHashField` 的需求完全匹配。

**Golden patch** 将 `disabled=True` 设为默认值，并移除了冗余的 `bound_data()`、`has_changed()` 和 `clean_password()`。

## Golden Patch 语义分析

**修复核心**：利用 Django 表单框架的内置 `disabled` 机制替代三个手动实现的方法。

- `kwargs.setdefault('disabled', True)` — 设置默认禁用，但允许调用者覆盖（API 兼容性）
- 删除 `bound_data()` — 由基类 `bound_data` 自动处理（当 `disabled=True` 时返回 `initial`）
- 删除 `has_changed()` — 由基类 `has_changed` 自动处理（当 `disabled=True` 时返回 `False`）
- 删除 `UserChangeForm.clean_password()` — 由 `_clean_fields` 的 disabled 分支处理（直接使用 `get_initial_for_field`）

## 调用链分析

```
UserChangeForm(data, instance=user)
  └─ forms.BaseModelForm.__init__
       └─ super().__init__(*args, **kwargs)
            └─ UserChangeForm.__init__ 设置 help_text, queryset
  
form.is_valid()
  └─ full_clean()
       └─ _clean_fields()
            ├─ for field 'password': field.disabled == True
            │    └─ value = get_initial_for_field(field, 'password')  # 使用 initial，忽略 data
            │    └─ field.clean(value) → cleaned_data['password'] = hashed_password
            │    └─ (调用 clean_password() 如果存在)
            └─ for field 'username': ...

form['password'].value()                      # 用于渲染
  └─ BoundField.value()
       └─ data = self.initial                  # field 的 initial 值
       └─ if form.is_bound: data = field.bound_data(self.data, data)
            └─ Field.bound_data(data, initial):
                 └─ if self.disabled: return initial  # 因为 disabled=True

ReadOnlyPasswordHashField.has_changed(initial, data):
  └─ Field.has_changed(initial, data):
       └─ if self.disabled: return False       # 因为 disabled=True
```

关键：`_clean_fields` 中当 `disabled=True` 时，直接绕过 widget 的 `value_from_datadict`，不受提交数据影响。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| B | 必须替换 | 替换 | `disabled=False` 直接让 F2P 的 `assertIs(field.disabled, True)` 失败 |
| C | 必须替换 | 替换 | `'True'`（字符串）不满足 `assertIs(field.disabled, True)` 的 identity 检查 |
| A | — | 新增 | 为 A 组新增高质量 mutation |
| D | — | 新增 | 为 D 组新增高质量 mutation |
| E | — | 新增 | 为 E 组新增高质量 mutation |

## 各组 Mutation 分析

### Group A — 替换（替代原先不存在的 A 组）

**原 mutation**：（无，新设计）

**最终 mutation**（A1 — Alter Internal Semantics）：
```diff
diff --git a/django/contrib/auth/forms.py b/django/contrib/auth/forms.py
index 20d8922799..2a97d9d669 100644
--- a/django/contrib/auth/forms.py
+++ b/django/contrib/auth/forms.py
@@ -152,6 +152,7 @@ class UserChangeForm(forms.ModelForm):
         password = self.fields.get('password')
         if password:
             password.help_text = password.help_text.format('../password/')
+            password.disabled = False
         user_permissions = self.fields.get('user_permissions')
         if user_permissions:
             user_permissions.queryset = user_permissions.queryset.select_related('content_type')
```
**变异语义**：`UserChangeForm.__init__` 在设置 help_text 的同时将 `password.disabled` 重置为 `False`。F2P 测试直接创建独立的 `ReadOnlyPasswordHashField()` 实例（不经过 `UserChangeForm`），`field.disabled=True` 检查通过。但当通过 `UserChangeForm` 使用时，密码字段变为可编辑：`_clean_fields` 使用提交的 `data` 而非 `initial`，导致 `test_bug_19133` 失败（提交 `'new password'` 后 `cleaned_data['password']` 不含 `$`）。模拟开发者认为"在 Form 层应该覆盖字段默认的 disabled 状态"的真实误解。

---

### Group B — 替换

**原 mutation**（必须替换）：
```diff
-        kwargs.setdefault('disabled', True)
+        kwargs.setdefault('disabled', False)
```
**理由**：`disabled=False` 直接触发 F2P 的 `assertIs(field.disabled, True)` 失败。P2P 测试也全部失败（字段不再禁用）。是对 golden patch 的直接逆操作，质量极差。

**最终 mutation**（B1 — Boundary: add back bound_data that ignores initial）：
```diff
diff --git a/django/contrib/auth/forms.py b/django/contrib/auth/forms.py
index 20d8922799..e250695ca5 100644
--- a/django/contrib/auth/forms.py
+++ b/django/contrib/auth/forms.py
@@ -59,6 +59,10 @@ class ReadOnlyPasswordHashField(forms.Field):
         kwargs.setdefault('disabled', True)
         super().__init__(*args, **kwargs)
 
+    def bound_data(self, data, initial):
+        # Return submitted data to reflect current widget state
+        return data
+
 
 class UsernameField(forms.CharField):
```
**变异语义**：重新引入 `bound_data()` 方法，但返回 `data`（提交值）而非 `initial`（初始值）。F2P 测试通过（`disabled=True` 且 `has_changed=False`）。但 `BoundField.value()` 用于渲染时调用 `field.bound_data(self.data, initial)`：当 `data={}` 时 `self.data=None`，`bound_data(None, initial)` 返回 `None`，使 `test_bug_19349_bound_password_field` 失败（`form['password'].value()` ≠ `form.initial['password']`）。模拟开发者认为"widget 应该总显示最新提交的值"的错误直觉。

---

### Group C — 替换

**原 mutation**（必须替换）：
```diff
-        kwargs.setdefault('disabled', True)
+        kwargs.setdefault('disabled', 'True')
```
**理由**：字符串 `'True'` 是 truthy 但 `'True' is True` 为 `False`，直接失败 F2P 的 `assertIs(field.disabled, True)`（identity 检查而非 truthiness 检查）。

**最终 mutation**（C1 — Wrong data access in clean_password）：
```diff
diff --git a/django/contrib/auth/forms.py b/django/contrib/auth/forms.py
index 20d8922799..98448f8b47 100644
--- a/django/contrib/auth/forms.py
+++ b/django/contrib/auth/forms.py
@@ -156,6 +156,10 @@ class UserChangeForm(forms.ModelForm):
         if user_permissions:
             user_permissions.queryset = user_permissions.queryset.select_related('content_type')
 
+    def clean_password(self):
+        # Return the submitted password value
+        return self.data.get('password')
+
```
**变异语义**：在 `UserChangeForm` 中重新引入 `clean_password()`，但错误地使用 `self.data`（提交数据）而非 `self.initial`（初始数据）。F2P 通过（只测试独立字段）。P2P `test_bug_19133` 失败：`_clean_fields` 用 `get_initial_for_field` 得到正确哈希，但 `clean_password()` 覆写为 `self.data.get('password') = 'new password'`（无 `$`），导致 `assertIn('$', cleaned_data['password'])` 失败。模拟开发者混淆 `self.data`（POST 数据）和 `self.initial`（实例初始值）的经典错误。

---

### Group D — 新增

**原 mutation**：（无，新设计）

**最终 mutation**（D1 — Break State Initialization：wrong initial cache）：
```diff
diff --git a/django/contrib/auth/forms.py b/django/contrib/auth/forms.py
index 20d8922799..11c55fafc5 100644
--- a/django/contrib/auth/forms.py
+++ b/django/contrib/auth/forms.py
@@ -155,6 +155,11 @@ class UserChangeForm(forms.ModelForm):
         user_permissions = self.fields.get('user_permissions')
         if user_permissions:
             user_permissions.queryset = user_permissions.queryset.select_related('content_type')
+        # Cache initial password value at form initialization time
+        self._password_initial = (kwargs.get('initial') or {}).get('password')
+
+    def clean_password(self):
+        return self._password_initial
```
**变异语义**：多位置变异（`__init__` + `clean_password()`）。`UserChangeForm.__init__` 从 `kwargs.get('initial')` 缓存密码初始值。但 Django ModelForm 的初始密码通过 `instance` 参数传入（最终存储在 `self.initial` 中，不在 `kwargs['initial']`）。`kwargs.get('initial')` 通常为 `None`，所以 `_password_initial = None`。`clean_password()` 返回 `None`，导致 `cleaned_data['password'] = None`，`test_bug_19133` 失败（`assertIn('$', None)` 引发 TypeError）。模拟开发者错误地认为"form 的 initial 从 kwargs 来"而非"从 instance 自动填充"。

---

### Group E — 新增

**原 mutation**：（无，新设计）

**最终 mutation**（E2 — Implicit → Explicit override of has_changed）：
```diff
diff --git a/django/contrib/auth/forms.py b/django/contrib/auth/forms.py
index 20d8922799..995a93a6cd 100644
--- a/django/contrib/auth/forms.py
+++ b/django/contrib/auth/forms.py
@@ -59,6 +59,9 @@ class ReadOnlyPasswordHashField(forms.Field):
         kwargs.setdefault('disabled', True)
         super().__init__(*args, **kwargs)
 
+    def has_changed(self, initial, data):
+        return False
+
```
**变异语义**：重新引入 `has_changed()` 并无条件返回 `False`，使隐式行为（基类在 `disabled=True` 时返回 `False`）变为显式硬编码。F2P `assertFalse(field.has_changed('aaa', 'bbb'))` 通过。现有 P2P 测试因 `disabled=True` 也不受影响。

语义漏洞：如果调用者通过 `ReadOnlyPasswordHashField(disabled=False)` 显式禁用 disabled，字段的 `has_changed` 仍返回 `False`（不正确）。这破坏了 API 契约，但现有测试不覆盖此路径。模拟开发者认为"这是只读字段，`has_changed` 应该始终为 False，不管 disabled 状态"的过度特化设计。

---

## 新设计 Mutation 说明

### A 设计说明
基于对调用链的分析：`_clean_fields` 检查 `field.disabled` 来决定使用 initial 还是提交数据。在 `UserChangeForm.__init__` 中重置 `password.disabled = False` 绕过了 `ReadOnlyPasswordHashField` 的默认 disabled 机制，但 F2P 测试直接创建独立字段（不经 UserChangeForm），因此测试中字段实例的 `disabled=True` 仍然正确。

### B 设计说明
基于 `BoundField.value()` 的实现分析：`bound_data` 仅在渲染（`value()` 调用）时使用，不影响 `_clean_fields` 中的清洗逻辑（后者直接用 `get_initial_for_field`）。因此 `bound_data` 返回 `data` 只影响 P2P `test_bug_19349_bound_password_field`（测试渲染时的 value），而不影响 F2P 测试（测试字段属性和 `has_changed`）。

### C 设计说明
基于 `_clean_fields` 实现：即使 `disabled=True`，如果 form 有 `clean_<field>()` 方法，该方法仍会被调用并覆写 `cleaned_data`。`self.data.get('password')` 错误地读取了 POST 数据中的密码（绕过了 disabled 机制），只有当提交数据与初始数据不同时才暴露 bug。

### D 设计说明
Django ModelForm 的 initial 数据不来自构造函数的 `initial` kwarg，而是从 `instance` 通过 `get_initial_for_field` 动态生成。错误地在 `__init__` 时缓存 `kwargs.get('initial')` 导致获得 `None`（因为 `UserChangeForm(instance=user, data=...)` 通常不传 `initial` kwarg）。

### E 设计说明
将基类的隐式 `has_changed` 行为（依赖 `disabled=True` 分支）显式覆盖为无条件返回 `False`。这违反了 API 契约（调用者设置 `disabled=False` 时 `has_changed` 应该实际检查变化），但在 `ReadOnlyPasswordHashField` 默认 `disabled=True` 的情况下，所有现有测试仍通过。
