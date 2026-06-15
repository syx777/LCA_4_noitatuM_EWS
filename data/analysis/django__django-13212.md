# django__django-13212

## 问题背景

Django 的内置 validators 在抛出 `ValidationError` 时没有传递 `params={'value': value}`，导致用户无法在自定义错误消息中使用 `%(value)s` 占位符来显示被验证的值。例如想要错误消息 `'"blah" is not a valid email.'` 是无法实现的。

Golden patch 在所有内置 validators 的 `ValidationError` 调用中统一添加 `params={'value': value}`（或在已有 params 中加入 `'value': value`），同时还移除了 `FloatField` 中冗余的 `validate()` 方法。

## Golden Patch 语义分析

核心改动模式：

```python
# 修复前：
raise ValidationError(self.message, code=self.code)

# 修复后：
raise ValidationError(self.message, code=self.code, params={'value': value})
```

以及对已有 params 的修改：
```python
# 修复前：
params={'max': self.max_digits}

# 修复后：
params={'max': self.max_digits, 'value': value}
```

`params` 字典在 Django 表单验证中用于将错误消息中的占位符替换为实际值。添加 `'value': value` 后，开发者可以在 `error_messages` 中使用 `%(value)s` 来显示被拒绝的输入值。

## 调用链分析

```
用户提交表单
  → Field.run_validators(value)
      → validator(value)  # 例如 EmailValidator.__call__
          → raise ValidationError(message, code='invalid', params={'value': value})
  → Field.validate(value)  # 内置验证
  
Form.errors 收集 ValidationError
  → Error message 模板渲染: 
    message % params → '%(value)s' % {'value': 'bad_input'} = 'bad_input'

F2P test:
  MyForm({'field': 'a'}) with error_messages={'invalid': '%(value)s'}
  → form.is_valid() = False
  → form.errors == {'field': ['a']}  ← 需要 params={'value': 'a'} 才能渲染
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 修改 int_list_validator 默认值为 allow_negative=True，破坏 P2P 测试（期望拒绝 '-1,2,3'） |
| B | 🔴 必须替换 | 替换 | 反转 RegexValidator.inverse_match 逻辑，破坏 P2P 测试（RegexValidator('[0-9]+') 不再拒绝 'xxxxxx'） |
| C | 缺失 | 新设计 | C 组缺失，需新设计 |
| D | 🟡 保留 | 保留 | 在 FileExtensionValidator.__init__ 移除 `if code is not None:` 守卫，功能性变化不破坏 P2P |
| E | 🔴 必须替换 | 替换 | 直接移除 RegexValidator 中的 params={'value': value}，是 golden patch 的直接逆操作 |

三个语义浅层（A/B/E 中 E 最弱，A/B 破坏 P2P），保留 D（功能性，不破坏 P2P）。

## 各组 Mutation 分析

### Group A — 替换

**最终 mutation**：
```diff
diff --git a/django/core/validators.py b/django/core/validators.py
index 830b533848..cb8f969c6c 100644
--- a/django/core/validators.py
+++ b/django/core/validators.py
@@ -208,7 +208,7 @@ class EmailValidator:
 
     def __call__(self, value):
         if not value or '@' not in value:
-            raise ValidationError(self.message, code=self.code, params={'value': value})
+            raise ValidationError(self.message, code=self.code, params={'val': value})
 
         user_part, domain_part = value.rsplit('@', 1)
```

**分类**：A1（错误的 params 字典键名）

**变异语义**：将 `EmailValidator` 第一个校验失败路径中的 `params={'value': value}` 改为 `params={'val': value}`。F2P 测试 `test_value_placeholder_with_char_field` 中有 `(validators.validate_email, 'a', 'invalid')` 的子测试：error_messages 使用 `%(value)s` 占位符，但 params 中没有 `'value'` 键 → 渲染失败（`KeyError` 或保留原始 `%(value)s` 字面量）→ `form.errors != {'field': ['a']}` → 测试失败。P2P 安全：EmailValidator 仍然抛出 ValidationError → P2P 测试只检查是否抛出异常，不检查 params 内容。

---

### Group B — 替换

**最终 mutation**：
```diff
diff --git a/django/core/validators.py b/django/core/validators.py
index 830b533848..d2b5485294 100644
--- a/django/core/validators.py
+++ b/django/core/validators.py
@@ -272,7 +272,7 @@ def validate_ipv4_address(value):
     try:
         ipaddress.IPv4Address(value)
     except ValueError:
-        raise ValidationError(_('Enter a valid IPv4 address.'), code='invalid', params={'value': value})
+        raise ValidationError(_('Enter a valid IPv4 address.'), code='invalid', params={'input': value})
```

**分类**：B3（错误的 params 字典键名：'value' → 'input'）

**变异语义**：`validate_ipv4_address` 中将 params 键名从 `'value'` 改为 `'input'`。F2P 测试中有 `(validators.validate_ipv4_address, '256.1.1.1', 'invalid')` 子测试：`%(value)s` 占位符找不到 `'value'` 键（只有 `'input'`）→ 渲染失败 → `form.errors != {'field': ['256.1.1.1']}` → 测试失败。P2P 安全：验证失败时仍抛出 ValidationError，P2P 测试不检查 params 内容。`'input'` 是一个合理的键名（来自 HTML input 元素的联想），让 bug 看起来像是故意选择的。

---

### Group C — 新设计

**最终 mutation**：
```diff
diff --git a/django/core/validators.py b/django/core/validators.py
index 830b533848..834b05b765 100644
--- a/django/core/validators.py
+++ b/django/core/validators.py
@@ -551,7 +551,7 @@ class ProhibitNullCharactersValidator:
 
     def __call__(self, value):
         if '\x00' in str(value):
-            raise ValidationError(self.message, code=self.code, params={'value': value})
+            raise ValidationError(self.message, code=self.code, params={})
 
     def __eq__(self, other):
         return (
```

**分类**：C1（空 params 字典）

**变异语义**：`ProhibitNullCharactersValidator` 中将 `params={'value': value}` 改为 `params={}`。F2P 测试 `test_value_placeholder_with_null_character` 使用 `error_messages={'null_characters_not_allowed': '%(value)s'}`，期望 `form.errors == {'field': ['a\x00b']}`。空 params 导致 `%(value)s` 渲染时找不到键 → 渲染失败或 KeyError → 错误消息不等于原始值 → 测试失败。P2P 安全：`ProhibitNullCharactersValidator` 仍然检测并拒绝空字符，P2P 测试不检查 params 内容。

---

### Group D — 保留

**原 mutation**（保留）：
```diff
-        if code is not None:
-            self.code = code
+        self.code = code
```

**分类**：D1（移除 FileExtensionValidator.__init__ 中的 None 守卫）

**变异语义**：移除 `if code is not None:` 守卫，使 `self.code = code` 在 `code=None` 时将实例属性设为 `None`（覆盖类级默认值 `'invalid_extension'`）。当 `validate_image_file_extension` 用默认参数创建时，`self.code = None`。F2P 测试 `test_value_placeholder_with_file_field` 使用 `error_messages={'invalid_extension': '%(value)s'}`，但 ValidationError 的 code 是 None → 无法映射到 `'invalid_extension'` 消息键 → 使用默认错误消息而非 `%(value)s` → `form.errors != {'field': ['myfile.txt']}` → 失败。P2P 安全：ValidationError 仍然抛出，P2P 测试只检查是否有 ValidationError。

---

### Group E — 替换

**最终 mutation**：
```diff
diff --git a/django/core/validators.py b/django/core/validators.py
index 830b533848..ea0c9bea47 100644
--- a/django/core/validators.py
+++ b/django/core/validators.py
@@ -438,7 +438,7 @@ class DecimalValidator:
     def __call__(self, value):
         digit_tuple, exponent = value.as_tuple()[1:]
         if exponent in {'F', 'n', 'N'}:
-            raise ValidationError(self.messages['invalid'], code='invalid', params={'value': value})
+            raise ValidationError(self.messages['invalid'], code='invalid')
```

**分类**：E2（完全移除 DecimalValidator 'invalid' 分支的 params）

**变异语义**：`DecimalValidator` 处理 NaN/Infinity 时移除 `params={'value': value}`。F2P 测试 `test_value_placeholder_with_decimal_field` 中有 `('NaN', 'invalid')` 子测试：`error_messages={'invalid': '%(value)s'}` 期望 `form.errors == {'field': ['NaN']}`。没有 params → `%(value)s` 渲染为字面量或 KeyError → 测试失败。P2P 安全：ValidationError 仍然抛出，P2P 测试只检查是否有 ValidationError。此 mutation 针对 DecimalValidator 的 'invalid'（NaN/Infinity）分支，而保留了 'max_digits'/'max_decimal_places'/'max_whole_digits' 的 params（这些仍有 value 但也有 max，所以不完全影响所有 decimal 测试）。
