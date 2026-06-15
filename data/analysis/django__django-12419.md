# django__django-12419

## 问题背景

Django 3.0 中加入了通过 `SECURE_REFERRER_POLICY` 设置发送 `Referrer-Policy` HTTP 头的功能，但默认值为 `None`（即不发送该头）。本 issue 提议将默认值改为 `'same-origin'`，以便 Django 应用默认保护用户隐私，避免将 Referer 信息泄露给第三方网站。

Golden patch 仅修改了一处：`django/conf/global_settings.py` 中将 `SECURE_REFERRER_POLICY = None` 改为 `SECURE_REFERRER_POLICY = 'same-origin'`。

## Golden Patch 语义分析

修复核心：将安全默认值从"禁用"改为"启用 same-origin 策略"。

- `same-origin` 策略：只有同源请求才会在 Referer 头中携带完整 URL，跨源请求不发送 Referer
- 在 `SecurityMiddleware.process_response()` 中，当 `self.referrer_policy` 不为 falsy 时，会向响应添加 `Referrer-Policy` 头
- 将默认值从 `None` 改为 `'same-origin'` 后，所有使用默认配置的 Django 项目都会自动发送该头

## 调用链分析

```
global_settings.py: SECURE_REFERRER_POLICY = 'same-origin'
  └── SecurityMiddleware.__init__()
        └── self.referrer_policy = settings.SECURE_REFERRER_POLICY   → 'same-origin'
  └── SecurityMiddleware.process_response(request, response)
        └── if self.referrer_policy:                                   → True
              └── response.setdefault('Referrer-Policy', ','.join(...))
                    └── 解析字符串/列表，设置响应头

check_referrer_policy() 函数:
  └── if settings.SECURE_REFERRER_POLICY is None: return [W022]       → 不再触发警告
  └── 验证 'same-origin' 在 REFERRER_POLICY_VALUES 中               → 合法值
```

数据流：`global_settings.SECURE_REFERRER_POLICY → SecurityMiddleware.referrer_policy → process_response → HTTP Referrer-Policy 头`

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 新设计 | 替换（新建） | 原数据无 A 组，需新设计 |
| B | 新设计 | 替换（新建） | 原数据无 B 组，需新设计 |
| C | 语义浅层 | 保留 | `same-origin` → `same_origin`（下划线），会触发 E023，是有效的浅层测试 |
| D | 语义浅层（最弱） | 替换 | `same-origin` → `same`，无效值，与 C 效果相似且检测位置相同，是3个浅层中最弱 |
| E | 语义浅层 | 保留 | `same-origin` → `no-referrer`，有效值但不同策略，改变安全语义 |

语义浅层共 3 个（C/D/E），替换其中最弱的 floor(3/2)=1 个：替换 D（`'same'` 与 `'same_origin'` 效果几乎相同，位置孤立，最容易被检测）。

## 各组 Mutation 分析

### Group A — 替换（新设计）
**原 mutation**：无（原数据中不存在 A 组）

**最终 mutation**：
```diff
diff --git a/django/middleware/security.py b/django/middleware/security.py
index c0877b350a..c07287eec0 100644
--- a/django/middleware/security.py
+++ b/django/middleware/security.py
@@ -44,7 +44,7 @@ class SecurityMiddleware(MiddlewareMixin):
         if self.xss_filter:
             response.setdefault('X-XSS-Protection', '1; mode=block')
 
-        if self.referrer_policy:
+        if self.referrer_policy and request.is_secure():
             # Support a comma-separated string or iterable of values to allow
             # fallback.
             response.setdefault('Referrer-Policy', ','.join(
```
**变异语义**：在 `SecurityMiddleware.process_response()` 中添加 `request.is_secure()` 条件，使 Referrer-Policy 头只在 HTTPS 请求下发送。对于 HTTP 请求（包括项目模板测试、开发环境），该头不会出现。代码看起来"合理"——开发者可能认为安全头只应随安全连接发送。但实际上 Referrer-Policy 对 HTTP 和 HTTPS 都有意义，且项目模板测试是 HTTP 请求，会触发测试失败。

---

### Group B — 替换（新设计）
**原 mutation**：无（原数据中不存在 B 组）

**最终 mutation**：
```diff
diff --git a/django/middleware/security.py b/django/middleware/security.py
index c0877b350a..dc66afaca0 100644
--- a/django/middleware/security.py
+++ b/django/middleware/security.py
@@ -15,7 +15,7 @@ class SecurityMiddleware(MiddlewareMixin):
         self.redirect = settings.SECURE_SSL_REDIRECT
         self.redirect_host = settings.SECURE_SSL_HOST
         self.redirect_exempt = [re.compile(r) for r in settings.SECURE_REDIRECT_EXEMPT]
-        self.referrer_policy = settings.SECURE_REFERRER_POLICY
+        self.referrer_policy = settings.SECURE_REFERRER_POLICY if settings.SECURE_HSTS_SECONDS else None
         self.get_response = get_response
```
**变异语义**：在 `SecurityMiddleware.__init__()` 中将 `referrer_policy` 与 HSTS 设置绑定——只有当 `SECURE_HSTS_SECONDS > 0` 时才实际使用 `referrer_policy`，否则为 `None`。由于默认 `SECURE_HSTS_SECONDS = 0`，绝大多数默认配置下 Referrer-Policy 头不会被发送。跨设置依赖，表面看像"安全策略整体联动"的合理设计，实际上破坏了 Referrer-Policy 的独立性。通过 `referrer_policy=None` 时的测试（因为它们明确 override settings），但项目模板集成测试会失败。

---

### Group C — 保留
**原 mutation**：
```diff
diff --git a/django/conf/global_settings.py b/django/conf/global_settings.py
index 8bb59a4037..58b13e5c5c 100644
--- a/django/conf/global_settings.py
+++ b/django/conf/global_settings.py
@@ -637,6 +637,6 @@ SECURE_HSTS_INCLUDE_SUBDOMAINS = False
 SECURE_HSTS_PRELOAD = False
 SECURE_HSTS_SECONDS = 0
 SECURE_REDIRECT_EXEMPT = []
-SECURE_REFERRER_POLICY = 'same-origin'
+SECURE_REFERRER_POLICY = 'same_origin'
 SECURE_SSL_HOST = None
 SECURE_SSL_REDIRECT = False
```
**分类**：🟡 语义浅层（保留）
**理由**：将连字符改为下划线，`'same_origin'` 不在 `REFERRER_POLICY_VALUES` 集合中，会触发 `E023` 错误（无效值）。能通过 `test_referrer_policy_off`（None 值测试）但失败于有效性检查测试。保留，因为它模拟了开发者将 HTTP 规范中的连字符命名混淆为 Python 下划线命名的错误。
**最终 mutation**：与原相同。
**变异语义**：导致 `check_referrer_policy` 返回 `E023`（无效策略），同时 `process_response` 会发送 `Referrer-Policy: same_origin` 头（实际上浏览器不识别此值）。

---

### Group D — 替换
**原 mutation**：
```diff
SECURE_REFERRER_POLICY = 'same'
```
**分类**：🟡 语义浅层（替换，最弱）
**理由**：`'same'` 同样是无效值，与 Group C (`'same_origin'`) 效果几乎相同（都触发 E023），修改位置完全相同，代码上的"错误类型"也相同，最容易被检测。在3个语义浅层中最弱，选择替换。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/django/conf/global_settings.py b/django/conf/global_settings.py
index 8bb59a4037..f40fad843d 100644
--- a/django/conf/global_settings.py
+++ b/django/conf/global_settings.py
@@ -637,6 +637,6 @@ SECURE_HSTS_INCLUDE_SUBDOMAINS = False
 SECURE_HSTS_PRELOAD = False
 SECURE_HSTS_SECONDS = 0
 SECURE_REDIRECT_EXEMPT = []
-SECURE_REFERRER_POLICY = 'same-origin'
+SECURE_REFERRER_POLICY = 'strict-origin-when-cross-origin'
 SECURE_SSL_HOST = None
 SECURE_SSL_REDIRECT = False
```
**变异语义**：将默认策略改为另一个合法但不同的策略 `'strict-origin-when-cross-origin'`。该值通过 `REFERRER_POLICY_VALUES` 验证，也会被正常发送，不触发安全警告。但测试 `test_middleware_headers` 期望响应头为 `Referrer-Policy: same-origin`，改为其他值会直接失败。同时有效性检查和中间件功能检查均通过，只有具体值匹配的测试失败。模拟了开发者在多个合法策略中选错了一个的情形。

---

### Group E — 保留
**原 mutation**：
```diff
SECURE_REFERRER_POLICY = 'no-referrer'
```
**分类**：🟡 语义浅层（保留）
**理由**：`'no-referrer'` 是合法的 Referrer-Policy 值，通过有效性检查，也会被正常设置为响应头，但语义完全不同（阻止所有 Referer 信息，而非同源限制）。项目模板测试期望 `Referrer-Policy: same-origin`，会失败于具体值断言。保留，因为它模拟了从多个安全策略中选择了更严格但不正确的选项的错误。
**最终 mutation**：与原相同。
**变异语义**：发送 `no-referrer` 策略会完全禁止发送 Referer 头，比 `same-origin` 更严格但语义不同。通过策略有效性检查和中间件功能测试，只失败于期望精确值的集成测试。

## 新设计 Mutation 说明

### Group A（新）：条件限制为 HTTPS 请求
分析 `process_response` 的逻辑，发现 `if self.referrer_policy:` 是唯一的发送门控。在其中添加 `request.is_secure()` 是很自然的"安全直觉"——许多其他安全机制（HSTS、SSL 重定向）都有 is_secure 检查。此 mutation 跨越了 `global_settings.py`（值定义）和 `middleware/security.py`（行为实现），在不同文件中制造 bug。大多数单元测试会 mock 或 override SECURE_REFERRER_POLICY，且用 HTTP 测试 referrer_policy=None 场景，不会触发此分支。项目模板集成测试用 HTTP 请求，是关键失败场景。

### Group B（新）：绑定 HSTS 配置
分析 `SecurityMiddleware.__init__()` 中各字段的初始化逻辑，`self.sts_seconds`、`self.referrer_policy` 等均独立赋值。将 `referrer_policy` 的启用与 `SECURE_HSTS_SECONDS` 绑定，模拟了开发者认为"Referrer-Policy 属于 HTTPS 安全加固套件，应与 HSTS 一同启用"的错误假设。代码风格上与同文件其他判断逻辑（如 `SECURE_SSL_REDIRECT`）一致，不易察觉。由于默认 `SECURE_HSTS_SECONDS=0`，这一修改会静默禁用 Referrer-Policy。
