# django__django-13195

## 问题背景

`HttpResponse.delete_cookie()` 在删除 cookie 时未传递 `samesite` 属性，导致浏览器（尤其是 Firefox）因 `samesite=none` 的 cookie 缺少 `secure` 标志而发出警告：`'Cookie "messages" will be soon rejected because it has the "sameSite" attribute set to "none" without the "secure" attribute'`。

根本原因：设置 cookie 时指定了 `samesite`，但删除 cookie（Set-Cookie with `Max-Age=0` + 过期时间）时没有传递 `samesite`，导致浏览器把删除操作的 Set-Cookie 头视为与设置时不同的 cookie（samesite 不匹配），而且对于 `samesite=none` 的场景也没有在删除时正确设置 `secure` 标志。

## Golden Patch 语义分析

修复涉及三个文件的三处改动：

```python
# django/http/response.py - delete_cookie() 改造
# 修复前（有 bug）：
def delete_cookie(self, key, path='/', domain=None):
    secure = key.startswith(('__Secure-', '__Host-'))
    self.set_cookie(key, max_age=0, ..., expires='Thu, 01 Jan 1970 00:00:00 GMT')

# 修复后：
def delete_cookie(self, key, path='/', domain=None, samesite=None):
    secure = (
        key.startswith(('__Secure-', '__Host-')) or
        (samesite and samesite.lower() == 'none')  # samesite=none 时强制 secure=True
    )
    self.set_cookie(key, max_age=0, ..., expires='...', samesite=samesite)
```

- **`samesite=None` 参数**：接受调用方传入的 samesite 值
- **`samesite.lower() == 'none'` 判断**：samesite=none 要求必须有 secure 标志，否则 cookie 被浏览器拒绝
- **`samesite=samesite` 传给 set_cookie**：删除 cookie 时保留原 samesite 属性，确保浏览器能匹配并正确删除

另外两处调用方修复：
- `django/contrib/sessions/middleware.py`：session 删除时传递 `samesite=SESSION_COOKIE_SAMESITE`
- `django/contrib/messages/storage/cookie.py`：messages 删除时传递 `samesite=SESSION_COOKIE_SAMESITE`

## 调用链分析

```
浏览器发送请求 (携带 sessionid cookie, samesite=None)
  → SessionMiddleware.process_response()
      session empty → response.delete_cookie(SESSION_COOKIE_NAME,
                                              path=SESSION_COOKIE_PATH,
                                              domain=SESSION_COOKIE_DOMAIN,
                                              samesite=SESSION_COOKIE_SAMESITE)
  → HttpResponseBase.delete_cookie(key='sessionid', samesite='None')
      secure = (False or ('None'.lower() == 'none')) = True
      self.set_cookie('sessionid', max_age=0, secure=True, expires=..., samesite='None')
  → Set-Cookie: sessionid=""; Max-Age=0; SameSite=None; Secure  ← 正确删除 cookie
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 缺失 | 新设计 | A 组缺失，设计跨多部分的高质量 mutation |
| B | 缺失 | 新设计 | B 组缺失 |
| C | 🔴 必须替换 | 替换 | 移除 set_cookie 中的 samesite 参数，是 golden patch 核心改动的直接逆操作 |
| D | 🔴 必须替换 | 替换 | 同时移除 expires 和 samesite，破坏 P2P test_default（expires 检查） |
| E | 🔴 必须替换 | 替换 | 添加无用参数 preserve_samesite=False，不自然且破坏 F2P（samesite 永远 None） |

## 各组 Mutation 分析

### Group A — 新设计

**最终 mutation**：
```diff
diff --git a/django/http/response.py b/django/http/response.py
index c0ed93c44e..22ead78dd4 100644
--- a/django/http/response.py
+++ b/django/http/response.py
@@ -215,10 +215,7 @@ class HttpResponseBase:
         # the secure flag and:
         # - the cookie name starts with "__Host-" or "__Secure-", or
         # - the samesite is "none".
-        secure = (
-            key.startswith(('__Secure-', '__Host-')) or
-            (samesite and samesite.lower() == 'none')
-        )
+        secure = key.startswith(('__Secure-', '__Host-'))
         self.set_cookie(
             key, max_age=0, path=path, domain=domain, secure=secure,
             expires='Thu, 01 Jan 1970 00:00:00 GMT', samesite=samesite,
```

**分类**：A1（移除 samesite='none' 时的安全标志逻辑）

**变异语义**：移除 `(samesite and samesite.lower() == 'none')` 条件。当用 `samesite='none'` 删除 cookie 时，`secure` 标志不再被自动设置为 True，导致浏览器拒绝处理该 Set-Cookie 头（samesite=none 要求 secure=True）→ F2P `test_delete_cookie_secure_samesite_none` 失败（expected True, got False）。samesite 属性仍被传递到 set_cookie（`samesite=samesite` 保留），其他 samesite 测试正常通过。P2P 安全：`__Secure-`/`__Host-` 前缀的 secure 检查不受影响。

---

### Group B — 新设计

**最终 mutation**：
```diff
diff --git a/django/http/response.py b/django/http/response.py
index c0ed93c44e..1556c5d81f 100644
--- a/django/http/response.py
+++ b/django/http/response.py
@@ -217,7 +217,7 @@ class HttpResponseBase:
         # - the samesite is "none".
         secure = (
             key.startswith(('__Secure-', '__Host-')) or
-            (samesite and samesite.lower() == 'none')
+            (samesite and samesite.lower() == 'never')
         )
```

**分类**：B2（检查字符串比较错误：`'none'` → `'never'`）

**变异语义**：将 `== 'none'` 改为 `== 'never'`。`samesite='none'` 时：`'none'.lower() == 'never'` → False → secure 不设置 → 删除 cookie 失败（浏览器拒绝）→ F2P `test_delete_cookie_secure_samesite_none` 失败。其他 samesite 测试（'lax'/'Strict'）不受影响。P2P 安全：字符串 `'never'` 不是合法的 samesite 值，标准 cookie 操作不受影响。难以发现：`'never'` 与 `'none'` 都是常见英文词，单词级别的 typo。

---

### Group C — 替换

**原 mutation**（来自 mutations.jsonl）：移除 `samesite=samesite` 参数（直接还原 golden patch 中的关键改动）

**分类**：🔴 必须替换（直接逆操作）

**最终 mutation**：
```diff
diff --git a/django/http/response.py b/django/http/response.py
index c0ed93c44e..aae7f763ee 100644
--- a/django/http/response.py
+++ b/django/http/response.py
@@ -217,7 +217,7 @@ class HttpResponseBase:
         # - the samesite is "none".
         secure = (
             key.startswith(('__Secure-', '__Host-')) or
-            (samesite and samesite.lower() == 'none')
+            (samesite and samesite.lower() != 'none')
         )
```

**分类**：C1（反转比较运算符：`==` → `!=`）

**变异语义**：将 `== 'none'` 改为 `!= 'none'`，反转逻辑：samesite 有值但不是 `'none'` 时设置 secure=True；samesite='none' 时 **不设置** secure=True。这完全颠倒了语义：现在只有非 none 的 samesite 才触发 secure，而 `'none'` 这个最需要 secure 的情形反而不触发 → F2P `test_delete_cookie_secure_samesite_none` 失败。另外：`delete_cookie('c', samesite='lax')` 现在会设置 secure=True（反而多设置了），可能导致其他测试失败，但 P2P 测试均基于未带 samesite 的调用，影响有限。

---

### Group D — 替换

**原 mutation**（来自 mutations.jsonl）：移除整个 `expires+samesite` 行，破坏 P2P test_default（检查 expires）

**分类**：🔴 必须替换（破坏 P2P）

**最终 mutation**：
```diff
diff --git a/django/contrib/sessions/middleware.py b/django/contrib/sessions/middleware.py
index 95ad30ce7f..63013eef7a 100644
--- a/django/contrib/sessions/middleware.py
+++ b/django/contrib/sessions/middleware.py
@@ -42,7 +42,6 @@ class SessionMiddleware(MiddlewareMixin):
                 settings.SESSION_COOKIE_NAME,
                 path=settings.SESSION_COOKIE_PATH,
                 domain=settings.SESSION_COOKIE_DOMAIN,
-                samesite=settings.SESSION_COOKIE_SAMESITE,
             )
             patch_vary_headers(response, ('Cookie',))
         else:
```

**分类**：D3（跨文件 session middleware 缺少 samesite 参数）

**变异语义**：在 session middleware 的 `delete_cookie()` 调用中移除 `samesite=SESSION_COOKIE_SAMESITE` 参数。session cookie 被删除时不包含 samesite 属性 → F2P session 测试（检查删除的 session cookie 是否包含正确 samesite 头）失败。P2P 安全：旧版本的 session 删除测试不检查 samesite 属性，都能通过。修改在独立文件中，不影响 response.py 的核心 delete_cookie 逻辑。

---

### Group E — 替换

**原 mutation**（来自 mutations.jsonl）：添加 `preserve_samesite=False` 参数使 samesite 永远为 None（不自然）

**分类**：🔴 必须替换（不自然）

**最终 mutation**：
```diff
diff --git a/django/contrib/messages/storage/cookie.py b/django/contrib/messages/storage/cookie.py
index b51e292aa0..84f719302b 100644
--- a/django/contrib/messages/storage/cookie.py
+++ b/django/contrib/messages/storage/cookie.py
@@ -95,7 +95,7 @@ class CookieStorage(BaseStorage):
             response.delete_cookie(
                 self.cookie_name,
                 domain=settings.SESSION_COOKIE_DOMAIN,
-                samesite=settings.SESSION_COOKIE_SAMESITE,
+                samesite=settings.SESSION_COOKIE_PATH,
             )
```

**分类**：E2（使用错误的 settings 属性：SESSION_COOKIE_PATH 代替 SESSION_COOKIE_SAMESITE）

**变异语义**：messages cookie 删除时传递 `SESSION_COOKIE_PATH`（值为 `'/'`）作为 samesite，而非 `SESSION_COOKIE_SAMESITE`（值为 `'Lax'`）。F2P `test_cookie_settings` 检查 `response.cookies['messages']['samesite'] == SESSION_COOKIE_SAMESITE`：`'/' != 'Lax'` → 测试失败。同时：`samesite='/'` 可能触发浏览器警告（非法 samesite 值），但不崩溃（`'/'.lower() == 'none'` 为 False → secure 不受影响）。P2P 安全：其他 messages 测试不检查删除 cookie 的 samesite 头。模拟了"复制粘贴时选错了 settings 属性"的真实开发错误。
