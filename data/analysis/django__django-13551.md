# django__django-13551

## 问题背景

用户通过邮箱请求密码重置后，如果该邮箱账户更改了邮件地址，之前生成的密码重置 token 仍然有效——这是一个安全漏洞。正确行为应是：一旦用户更改邮件地址，所有之前发出的密码重置 token 应立即失效。

根本原因：`PasswordResetTokenGenerator._make_hash_value()` 在生成 token 哈希时，只包含 `user.pk`、`user.password`、`last_login` 和时间戳，**没有包含用户的邮件地址**。因此即使邮件地址变更，hash 也不会变化，旧 token 依然可用。

Golden patch 修复：在 `_make_hash_value` 中增加两行，通过 `user.get_email_field_name()` 获取邮件字段名，再用 `getattr(user, email_field, '') or ''` 读取字段值（`or ''` 处理 None 值），最终将 email 纳入 f-string 哈希串。

## Golden Patch 语义分析

核心修复是在 `_make_hash_value` 的返回值中追加 `{email}`：

```python
email_field = user.get_email_field_name()
email = getattr(user, email_field, '') or ''
return f'{user.pk}{user.password}{login_timestamp}{timestamp}{email}'
```

关键设计决策：
1. **使用 `get_email_field_name()`**：不同 User 模型的邮件字段名可能不同（如标准 User 用 `email`，自定义模型可能用 `email_address`），必须通过方法获取字段名，不能硬编码。
2. **使用 `getattr(user, email_field, '') or ''`**：双重保障——若模型没有该字段则返回默认 `''`；若字段值为 `None`（null=True 的字段）则 `or ''` 将其转为 `''`，确保哈希一致性。
3. **拼接进 f-string**：追加到末尾不影响现有 token 格式的其他部分，但一旦 email 变化，整个哈希串不同，旧 token 立即失效。

## 调用链分析

```
make_token(user)
    └─> _make_token_with_timestamp(user, timestamp)
            └─> _make_hash_value(user, timestamp)   [← golden patch 修改此处]
                    ├─ user.get_email_field_name()  [AbstractBaseUser classmethod]
                    │       └─ 返回 cls.EMAIL_FIELD 或 'email'（fallback）
                    └─ getattr(user, email_field, '') or ''

check_token(user, token)
    ├─> 解析 ts_b36 → ts
    └─> _make_token_with_timestamp(user, ts)  [重新生成哈希与 token 比对]
            └─> _make_hash_value(user, ts)   [使用当前 user 状态]
```

数据流：
- `user.get_email_field_name()` 是类方法，返回 `cls.EMAIL_FIELD`（若定义）或 `'email'`（默认）
- `getattr(user, email_field, '')` 在实例上动态查找字段值，`or ''` 处理 None
- email 字符串最终追加到哈希串末尾
- `check_token` 重新调用 `_make_hash_value` 时使用**当前用户状态**的 email，若 email 已变则哈希不同

## 替换决策总览

| 组 | 原有 Mutation | 分类 | 决策 | 原因摘要 |
|---|---|---|---|---|
| A | （不存在，新设计） | — | 新设计 | mutations.jsonl 中仅有 B/C 两组，需补全 A/D/E |
| B | `'' if getattr(...) else getattr(...)` | 🟢 保留 | 保留 | 逻辑取反，有效使非空 email 不进入哈希，F2P 失败 |
| C | `getattr(user, email_field, '')` (无 or '') | 🔴 必须替换 | 替换 | email 为 None 时输出字符串 'None' 与 '' 不同，但 email 变化仍能改变哈希，所有 F2P 测试仍 PASS，是功能等价冗余 |
| D | （不存在，新设计） | — | 新设计 | 同 A |
| E | （不存在，新设计） | — | 新设计 | 同 A |

语义浅层共 0 个；必须替换 1 个（C）；新设计 3 个（A/D/E）

## 各组 Mutation 分析

### Group A — 新设计（补全）
**分类**：新设计（A1 类型：使用错误属性名进行 getattr）
**设计思路**：将 `getattr(user, email_field, '') or ''` 中的 `email_field` 变量替换为硬编码字符串 `'email'`。标准 `User` 模型的 `EMAIL_FIELD = 'email'`，所以对标准用户行为一致；但对使用自定义邮件字段名的模型（如 `CustomEmailField` 的 `email_address`），`user.email` 属性不存在，`getattr` 返回默认 `''`，导致 email 变化不影响 hash。
**最终 mutation**：
```diff
diff --git a/django/contrib/auth/tokens.py b/django/contrib/auth/tokens.py
index c534f304f3..b4103957bd 100644
--- a/django/contrib/auth/tokens.py
+++ b/django/contrib/auth/tokens.py
@@ -95,7 +95,7 @@ class PasswordResetTokenGenerator:
         # database doesn't support microseconds.
         login_timestamp = '' if user.last_login is None else user.last_login.replace(microsecond=0, tzinfo=None)
         email_field = user.get_email_field_name()
-        email = getattr(user, email_field, '') or ''
+        email = getattr(user, 'email', '') or ''
         return f'{user.pk}{user.password}{login_timestamp}{timestamp}{email}'
 
     def _num_seconds(self, dt):
```
**变异语义**：email_field 变量被计算出来但未使用，改用硬编码 `'email'`。对标准 User 模型（`EMAIL_FIELD='email'`）完全透明；对 `CustomEmailField` 模型（`EMAIL_FIELD='email_address'`），`getattr(user, 'email', '')` 始终为 `''`，email 变化不影响 hash。F2P 测试子用例 `(CustomEmailField, None)` 和 `(CustomEmailField, 'test4@...')` 会失败。

---

### Group B — 保留
**原 mutation**：
```diff
-        email = getattr(user, email_field, '') or ''
+        email = '' if getattr(user, email_field, '') else getattr(user, email_field, '')
```
**分类**：🟢 保留（逻辑取反类，位置关键，效果明显但不直观）
**理由**：`'' if X else X` 在 X 为真值时返回 `''`，在 X 为假值时返回 X。这将逻辑颠倒：有实际 email（真值）时 hash 包含 `''`，无 email（假值）时 hash 包含 `None` 或 `''`。结果是对于有非空 email 的用户，email 变化（两个不同非空值之间）不会改变 hash。F2P 测试子用例 `(CustomEmailField, 'test4@')` 和 `(User, 'test4@')` 会失败。这是一个有效的高质量 mutation。
**最终 mutation**（与原相同）：
```diff
diff --git a/django/contrib/auth/tokens.py b/django/contrib/auth/tokens.py
index c534f304f3..d52b30473f 100644
--- a/django/contrib/auth/tokens.py
+++ b/django/contrib/auth/tokens.py
@@ -95,7 +95,7 @@ class PasswordResetTokenGenerator:
         # database doesn't support microseconds.
         login_timestamp = '' if user.last_login is None else user.last_login.replace(microsecond=0, tzinfo=None)
         email_field = user.get_email_field_name()
-        email = getattr(user, email_field, '') or ''
+        email = '' if getattr(user, email_field, '') else getattr(user, email_field, '')
         return f'{user.pk}{user.password}{login_timestamp}{timestamp}{email}'
 
     def _num_seconds(self, dt):
```
**变异语义**：对于有实际 email 的用户，email 永远 hash 为 `''`；email 从一个有效地址变为另一个有效地址时 hash 不变，token 不失效。

---

### Group C — 替换
**原 mutation**：
```diff
-        email = getattr(user, email_field, '') or ''
+        email = getattr(user, email_field, '')
```
**分类**：🔴 必须替换（功能等价冗余）
**理由**：去掉 `or ''` 后，当 `email_address=None` 时，`getattr` 返回 `None`，f-string 中变为字符串 `'None'`。但 F2P 测试在 email 从 `None` 变为 `'test4new@...'` 时，`'None'` 与 `'test4new@...'` 不同，hash 依然变化，token 仍被正确失效。三个 F2P 子测试均 PASS，此 mutation 是无效变异。

**替换后 mutation**（基于 last_login 条件）：
```diff
diff --git a/django/contrib/auth/tokens.py b/django/contrib/auth/tokens.py
index c534f304f3..18b06ac61f 100644
--- a/django/contrib/auth/tokens.py
+++ b/django/contrib/auth/tokens.py
@@ -95,7 +95,7 @@ class PasswordResetTokenGenerator:
         # database doesn't support microseconds.
         login_timestamp = '' if user.last_login is None else user.last_login.replace(microsecond=0, tzinfo=None)
         email_field = user.get_email_field_name()
-        email = getattr(user, email_field, '') or ''
+        email = (getattr(user, email_field, '') or '') if user.last_login is not None else ''
         return f'{user.pk}{user.password}{login_timestamp}{timestamp}{email}'
 
     def _num_seconds(self, dt):
```
**变异语义**：只有当用户有 `last_login` 记录时，才将 email 纳入 hash。F2P 测试中 `create_user` 创建的用户默认 `last_login=None`，所以 email 始终为 `''`，email 变化不影响 hash，token 不失效。所有3个 F2P 子测试失败。这模拟了一种"防御性"开发误区：认为没有登录历史的账户不需要通过邮件保护 token。

---

### Group D — 新设计（补全）
**分类**：新设计（D2 类型：使用 username 值作为属性名进行动态查找）
**设计思路**：将 `getattr(user, email_field, '')` 改为 `getattr(user, user.get_username(), '')`。`get_username()` 返回的是用户名的**值**（如 `'changeemailuser'`），不是字段名。`getattr(user, 'changeemailuser', '')` 查找名为 `'changeemailuser'` 的属性，该属性不存在，返回 `''`。email 字段变化不影响 hash。
**最终 mutation**：
```diff
diff --git a/django/contrib/auth/tokens.py b/django/contrib/auth/tokens.py
index c534f304f3..8d9ef49e0f 100644
--- a/django/contrib/auth/tokens.py
+++ b/django/contrib/auth/tokens.py
@@ -95,7 +95,7 @@ class PasswordResetTokenGenerator:
         # database doesn't support microseconds.
         login_timestamp = '' if user.last_login is None else user.last_login.replace(microsecond=0, tzinfo=None)
         email_field = user.get_email_field_name()
-        email = getattr(user, email_field, '') or ''
+        email = getattr(user, user.get_username(), '') or ''
         return f'{user.pk}{user.password}{login_timestamp}{timestamp}{email}'
 
     def _num_seconds(self, dt):
```
**变异语义**：`get_username()` 返回 username 的**值**（字符串），不是字段名。使用用户名值作为属性名进行查找，永远返回 `''`（不存在这样的属性），email 完全不纳入 hash。这模拟了一个开发者混淆 `get_username()`（返回值）和 `USERNAME_FIELD`（字段名）的错误，类似地也混淆了 `email_field`（字段名）和 `get_email_field_name()`（返回字段名）。所有3个 F2P 子测试失败。

---

### Group E — 新设计（补全）
**分类**：新设计（E1 类型：使用用户名字段名代替邮件字段名）
**设计思路**：将 `getattr(user, email_field, '')` 改为 `getattr(user, user.USERNAME_FIELD, '')`。`USERNAME_FIELD` 是字段名字符串（如 `'username'`），用它作为 getattr 的属性名，得到的是用户名的值，而非 email 值。用户名在 F2P 测试中不会变化，所以 email 变化不会影响 hash。
**最终 mutation**：
```diff
diff --git a/django/contrib/auth/tokens.py b/django/contrib/auth/tokens.py
index c534f304f3..e0d4d6fd38 100644
--- a/django/contrib/auth/tokens.py
+++ b/django/contrib/auth/tokens.py
@@ -95,7 +95,7 @@ class PasswordResetTokenGenerator:
         # database doesn't support microseconds.
         login_timestamp = '' if user.last_login is None else user.last_login.replace(microsecond=0, tzinfo=None)
         email_field = user.get_email_field_name()
-        email = getattr(user, email_field, '') or ''
+        email = getattr(user, user.USERNAME_FIELD, '') or ''
         return f'{user.pk}{user.password}{login_timestamp}{timestamp}{email}'
 
     def _num_seconds(self, dt):
```
**变异语义**：`user.USERNAME_FIELD` 是字符串 `'username'`，`getattr(user, 'username', '')` 返回的是用户名值（恒定不变）而非 email。将 email 字段名换为用户名字段名，使 hash 中包含用户名而非 email。用户名不变，email 改变时 hash 不变，token 不失效。所有3个 F2P 子测试失败。这模拟了开发者在实现"唯一标识用户"功能时，误将 `USERNAME_FIELD` 与 `EMAIL_FIELD`（通过 `get_email_field_name()` 获得）混用的错误。

## 新设计 Mutation 说明

### Group A — 硬编码 `'email'` 代替 `email_field`
基于代码分析：`get_email_field_name()` 的存在正是为了解耦不同 User 模型的邮件字段名。硬编码 `'email'` 是一个合理看起来的简化，因为标准 `User` 模型确实叫 `email`。这类错误在实际开发中常见：开发者看到 `email_field` 变量就在上面，然后想"我知道 User 模型的 email 字段叫 email"，于是直接写死。对 `CustomEmailField` 模型（`email_address` 字段）静默失效。

### Group C（替换）— 基于 last_login 的条件包含 email
基于 `_make_hash_value` 的注释阅读：注释说 `last_login` "通常会在密码重置后立即更新"，暗示 `last_login` 是 token 失效的重要依据。开发者可能认为：如果 `last_login=None`（未登录过），token 本身是"首次使用"的，不需要 email 作为额外保护；只有有登录历史的用户才需要 email 保护。这个逻辑看似有一定道理但是错误的——新注册用户（`last_login=None`）发出的密码重置 token 同样需要在 email 变化后失效。

### Group D — 使用 `get_username()` 值作为属性名
`get_username()` 和 `get_email_field_name()` 是相似的方法签名，一个获取用户名值，一个获取邮件字段名。开发者可能混淆这两者的语义，认为 `get_username()` 返回字段名而非字段值。与 Group E 的区别：D 使用的是方法调用（动态值），E 使用的是类属性（字段名）。两者都导致 email 不进入 hash，但切入角度不同。

### Group E — 使用 `USERNAME_FIELD` 代替 email_field
`user.USERNAME_FIELD`（用户名字段名）与 `email_field`（邮件字段名）是对称的设计。一个开发者可能认为"应该用能标识用户的字段来加固 token"，误将 `USERNAME_FIELD` 用于 email 的 getattr 查找。虽然用户名也是有意义的安全属性（username 变化也应失效 token），但这里的测试专门验证 email 变化的场景，username 不变则 hash 不变，测试失败。
