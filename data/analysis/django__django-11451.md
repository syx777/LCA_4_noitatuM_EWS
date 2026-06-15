# django__django-11451

## 问题背景

`ModelBackend.authenticate()` 在 `username` 为 `None` 或 `password` 为 `None` 时仍会执行数据库查询，浪费资源并可能暴露时序信息。

Issue 的核心：当调用方未提供凭据（如 `authenticate({})`、`authenticate(username=X)` 或 `authenticate(password='test')`），旧代码会先尝试从 `kwargs` 中解析 `username`，然后直接发起 `UserModel._default_manager.get_by_natural_key(username)` 查询——即使此时 `username` 或 `password` 为 `None`。

Golden patch 在 kwargs 解析之后、数据库查询之前插入一个提前返回的守卫条件：
```python
if username is None or password is None:
    return
```
从而确保两个凭据都存在时才发起查询。

## Golden Patch 语义分析

修复的核心逻辑：**两个凭据都必须非 None 才值得查数据库**。

- `username is None`：用户名字段缺失，无法定位用户记录，查询必然无意义或报错。
- `password is None`：密码字段缺失，即使找到用户也无法验证，查询是浪费。
- 该守卫放在 `kwargs` 解析之后，确保即便用户名通过 `kwargs` 传入也被正确处理。
- 副作用：同时避免了 `UserModel().set_password(password)` 被以 `None` 密码调用（时序保护逻辑也跟着短路）。

## 调用链分析

```
authenticate(**credentials)   [django/contrib/auth/__init__.py]
  └─ backend.authenticate(request, **credentials)
       └─ ModelBackend.authenticate(request, username, password, **kwargs)
            ├─ UserModel._default_manager.get_by_natural_key(username)   [DB query]
            │    └─ BaseUserManager.get_by_natural_key()  [base_user.py:43]
            ├─ UserModel().set_password(password)   [时序保护，DoesNotExist 路径]
            └─ user.check_password(password)   [验证路径]
                 └─ self.user_can_authenticate(user)
```

数据流：`credentials dict → kwargs 解析 → 守卫检查 → DB 查询 → 密码校验 → user_can_authenticate`

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 新设计（高质量） | 新增 | mutations.jsonl 中不存在 A 组，需全新设计 |
| B | 新设计（高质量） | 新增 | mutations.jsonl 中不存在 B 组，需全新设计 |
| C | 新设计（语义浅层） | 新增 | mutations.jsonl 中不存在 C 组，需全新设计 |
| D | 语义浅层 | 保留 | 修改位置关键（守卫条件），模拟"只关注 username 不关注 password"的真实失误 |
| E | 🔴 必须替换 | 替换 | `check_credentials=False` 默认参数使守卫永远不触发，等价于完全还原 bug，且 API 改动不自然 |

语义浅层共 1 个保留（D），C 组虽也是浅层但属新设计填充，不在替换计数范围内。

## 各组 Mutation 分析

### Group A — 新增（高质量）

**原 mutation**：无（A 组在 mutations.jsonl 中不存在）

**分类**：🟢 高质量（新设计）

**理由**：设计多行跨结构变异——将提前返回守卫从函数顶部移至 `else` 分支内部，同时修改 `DoesNotExist` 分支的时序保护逻辑。这模拟了开发者"只在找到用户时才检查凭据完整性"的错误理解。

**最终 mutation**：
```diff
diff --git a/django/contrib/auth/backends.py b/django/contrib/auth/backends.py
index a3765ae0f1..7fee8c304d 100644
--- a/django/contrib/auth/backends.py
+++ b/django/contrib/auth/backends.py
@@ -39,15 +39,16 @@ class ModelBackend(BaseBackend):
     def authenticate(self, request, username=None, password=None, **kwargs):
         if username is None:
             username = kwargs.get(UserModel.USERNAME_FIELD)
-        if username is None or password is None:
-            return
         try:
             user = UserModel._default_manager.get_by_natural_key(username)
         except UserModel.DoesNotExist:
             # Run the default password hasher once to reduce the timing
             # difference between an existing and a nonexistent user (#20760).
-            UserModel().set_password(password)
+            if password is not None:
+                UserModel().set_password(password)
         else:
+            if username is None or password is None:
+                return
             if user.check_password(password) and self.user_can_authenticate(user):
                 return user
```

**变异语义**：守卫条件被移至 `else` 分支，仅在用户存在（DB 查到结果）后才检查凭据完整性。当 `username` 有值但 `password=None` 时（如 `authenticate(username='alice')`），会先发起 DB 查询，然后在 `else` 分支检查 password 后才 return。`DoesNotExist` 分支也仅在 password 非 None 时调用 `set_password`。效果：`assertNumQueries(0)` 断言失败，且 `CountingMD5PasswordHasher.calls` 不变（因为 password=None 路径不触发 set_password）。代码读起来像是"只有找到用户才需要验证凭据完整性"的合理重构，难以在代码审查中发现。

---

### Group B — 新增（高质量，多行）

**原 mutation**：无（B 组在 mutations.jsonl 中不存在）

**分类**：🟢 高质量（新设计，多行修改）

**理由**：将守卫拆分——只保留 `username is None` 的 guard，同时将 `DoesNotExist` 分支的时序保护改为仅在 `password is not None` 时触发。模拟了开发者"password 为 None 时让查询继续但跳过 set_password"的错误逻辑，认为这样更"安全"。

**最终 mutation**：
```diff
diff --git a/django/contrib/auth/backends.py b/django/contrib/auth/backends.py
index a3765ae0f1..1df6b42c74 100644
--- a/django/contrib/auth/backends.py
+++ b/django/contrib/auth/backends.py
@@ -39,14 +39,15 @@ class ModelBackend(BaseBackend):
     def authenticate(self, request, username=None, password=None, **kwargs):
         if username is None:
             username = kwargs.get(UserModel.USERNAME_FIELD)
-        if username is None or password is None:
+        if username is None:
             return
         try:
             user = UserModel._default_manager.get_by_natural_key(username)
         except UserModel.DoesNotExist:
             # Run the default password hasher once to reduce the timing
             # difference between an existing and a nonexistent user (#20760).
-            UserModel().set_password(password)
+            if password is not None:
+                UserModel().set_password(password)
         else:
             if user.check_password(password) and self.user_can_authenticate(user):
                 return user
```

**变异语义**：username 守卫保留，但 password=None 时不再提前返回。当 `authenticate(username='alice')` 被调用（无 password），会查询 DB（`assertNumQueries(0)` FAILS），且 `DoesNotExist` 路径不会调用 `set_password`（`CountingMD5PasswordHasher.calls == 0` 仍然通过）。表面上看这像是把时序保护逻辑优化为"只在有密码时才需要时序保护"——是一个很隐蔽的逻辑错误。

---

### Group C — 新增（语义浅层）

**原 mutation**：无（C 组在 mutations.jsonl 中不存在）

**分类**：🟡 语义浅层（单行条件简化）

**理由**：仅保留 password 的 None 检查，移除 username 的检查。与 D 组互补——D 只检查 username，C 只检查 password。当 `authenticate(password='test')` 被调用（无 username），C 组的 `if password is None` 不会触发（password='test' 非 None），导致以 `username=None` 发起 DB 查询。

**最终 mutation**：
```diff
diff --git a/django/contrib/auth/backends.py b/django/contrib/auth/backends.py
index a3765ae0f1..78988425ad 100644
--- a/django/contrib/auth/backends.py
+++ b/django/contrib/auth/backends.py
@@ -39,7 +39,7 @@ class ModelBackend(BaseBackend):
     def authenticate(self, request, username=None, password=None, **kwargs):
         if username is None:
             username = kwargs.get(UserModel.USERNAME_FIELD)
-        if username is None or password is None:
+        if password is None:
             return
         try:
             user = UserModel._default_manager.get_by_natural_key(username)
```

**变异语义**：守卫条件从 `username is None or password is None` 简化为 `password is None`。对 `authenticate({})` 和 `authenticate(username=X)` 两个测试子用例均能正确短路（password=None），但 `authenticate(password='test')` 子用例（有密码无用户名）会绕过守卫，以 `username=None` 发起 DB 查询，导致 `assertNumQueries(0)` 失败。

---

### Group D — 保留

**原 mutation**：
```diff
diff --git a/django/contrib/auth/backends.py b/django/contrib/auth/backends.py
index a3765ae0f1..30308cb34e 100644
--- a/django/contrib/auth/backends.py
+++ b/django/contrib/auth/backends.py
@@ -39,7 +39,7 @@ class ModelBackend(BaseBackend):
     def authenticate(self, request, username=None, password=None, **kwargs):
         if username is None:
             username = kwargs.get(UserModel.USERNAME_FIELD)
-        if username is None or password is None:
+        if username is None:
             return
         try:
             user = UserModel._default_manager.get_by_natural_key(username)
```

**分类**：🟡 语义浅层（保留）

**理由**：条件简化为只检查 `username is None`，是关键守卫节点上的有效变异。模拟了"只关注 username 缺失，忽视 password 缺失"的真实失误。`authenticate(username='alice')` 子用例（有用户名无密码）会发起 DB 查询，导致 `assertNumQueries(0)` 失败。与 C 组形成互补，共同覆盖两种凭据缺失场景。

**最终 mutation**（与原相同）：
```diff
diff --git a/django/contrib/auth/backends.py b/django/contrib/auth/backends.py
index a3765ae0f1..30308cb34e 100644
--- a/django/contrib/auth/backends.py
+++ b/django/contrib/auth/backends.py
@@ -39,7 +39,7 @@ class ModelBackend(BaseBackend):
     def authenticate(self, request, username=None, password=None, **kwargs):
         if username is None:
             username = kwargs.get(UserModel.USERNAME_FIELD)
-        if username is None or password is None:
+        if username is None:
             return
         try:
             user = UserModel._default_manager.get_by_natural_key(username)
```

**变异语义**：password=None 时不再提前返回，导致 `authenticate(username=X, password=None)` 发起 DB 查询。此变异能通过大多数正常认证流程（正常用户名+密码），只在密码字段为 None 的特殊调用场景下失败。

---

### Group E — 替换

**原 mutation**（必须替换）：
```diff
diff --git a/django/contrib/auth/backends.py b/django/contrib/auth/backends.py
index a3765ae0f1..9e556f13ff 100644
--- a/django/contrib/auth/backends.py
+++ b/django/contrib/auth/backends.py
@@ -36,10 +36,10 @@ class ModelBackend(BaseBackend):
     Authenticates against settings.AUTH_USER_MODEL.
     """
 
-    def authenticate(self, request, username=None, password=None, **kwargs):
+    def authenticate(self, request, username=None, password=None, check_credentials=False, **kwargs):
         if username is None:
             username = kwargs.get(UserModel.USERNAME_FIELD)
-        if username is None or password is None:
+        if check_credentials and (username is None or password is None):
             return
```

**分类**：🔴 必须替换

**理由**：添加 `check_credentials=False` 默认参数，使守卫永远不触发（默认 False）。功能上完全等价于直接还原 base_commit 的原始 bug。同时，向公共 API 添加 `check_credentials` 参数是不自然的改动，在代码审查中会立即引起注意。

**最终 mutation**（新设计，request-conditional guard）：
```diff
diff --git a/django/contrib/auth/backends.py b/django/contrib/auth/backends.py
index a3765ae0f1..d4f62bd5ed 100644
--- a/django/contrib/auth/backends.py
+++ b/django/contrib/auth/backends.py
@@ -39,7 +39,7 @@ class ModelBackend(BaseBackend):
     def authenticate(self, request, username=None, password=None, **kwargs):
         if username is None:
             username = kwargs.get(UserModel.USERNAME_FIELD)
-        if username is None or password is None:
+        if request is not None and (username is None or password is None):
             return
         try:
             user = UserModel._default_manager.get_by_natural_key(username)
```

**变异语义**：守卫条件添加 `request is not None` 前提——只有在有 HTTP 请求上下文时才短路。而 `django.contrib.auth.authenticate()` 顶层函数默认 `request=None`，测试也通常不传 request，因此守卫永远不触发，所有三个无凭据子用例都会发起 DB 查询，导致 `assertNumQueries(0)` 失败。这模拟了"防御性检查只应在 HTTP 层面才有意义"的错误推理，逻辑上看起来有一定道理，难以在代码审查中被立即识别。

## 新设计 Mutation 说明

### Group A
基于对 `authenticate()` 完整流程的分析：try/except/else 三分支结构中，`else` 分支是"找到用户"路径。将守卫移入 `else` 分支模拟了"凭据完整性检查只在找到用户后才有意义"的误解。同时，`DoesNotExist` 分支的 `set_password` 被加了 `if password is not None` 条件，看起来像是合理的"防止 set_password(None) 调用"的优化，掩盖了真正的问题（提前返回被移走）。

### Group B
分析了 `DoesNotExist` 分支的时序保护逻辑。该分支调用 `UserModel().set_password(password)` 是为了让不存在的用户和存在用户的响应时间一致（防时序攻击）。将其改为 `if password is not None: UserModel().set_password(password)` 看似是合理的 None 安全处理，但实际上暗示了开发者认为"password=None 时不需要时序保护"——而这个认知的前提错误在于：password=None 时根本就不应该查数据库。这个多行修改将两个相关但不同的逻辑问题混在一起，难以快速识别。

### Group E (new)
分析了 `authenticate()` 调用链：`django.contrib.auth.authenticate(request=None, **credentials)` 中 request 默认为 None。大多数测试和编程式调用不传 request。因此 `if request is not None and (...)` 使守卫成为死代码。这个修改模拟了"HTTP 安全守卫只对真实 HTTP 请求有意义"的错误推理，语义上有一定迷惑性，且不改变任何函数签名，代码审查时需要仔细追踪 request 的传递链才能发现问题。
