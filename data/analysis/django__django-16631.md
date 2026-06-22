# django__django-16631

## 问题背景

`SECRET_KEY_FALLBACKS` 没有被用于会话（session）验证。用户轮换 SECRET_KEY、把旧值放进 `SECRET_KEY_FALLBACKS` 后，所有人被登出。根因：`AbstractBaseUser.get_session_auth_hash` 用 `salted_hmac` 时默认只用当前 `SECRET_KEY`，未尝试 fallback 密钥；`get_user` 在当前密钥验证会话失败时直接 `flush()` 登出，不回退尝试旧密钥。Golden patch 让 `get_session_auth_hash` 支持 `secret` 参数（新增 `_get_session_auth_hash(secret=None)` 与生成器 `get_session_auth_fallback_hash`），并在 `get_user` 中：当前密钥验证失败时遍历 fallback 密钥的 hash，命中则 `cycle_key()` 并把会话 hash 更新成当前密钥的，而非登出。

## Golden Patch 语义分析

```python
# base_user.py
def get_session_auth_hash(self):
    return self._get_session_auth_hash()
def get_session_auth_fallback_hash(self):
    for fallback_secret in settings.SECRET_KEY_FALLBACKS:
        yield self._get_session_auth_hash(secret=fallback_secret)
def _get_session_auth_hash(self, secret=None):
    key_salt = "...get_session_auth_hash"
    return salted_hmac(key_salt, self.password, secret=secret, algorithm="sha256").hexdigest()

# __init__.py get_user
if not session_hash_verified:
    if session_hash and any(
        constant_time_compare(session_hash, fallback_auth_hash)
        for fallback_auth_hash in user.get_session_auth_fallback_hash()
    ):
        request.session.cycle_key()
        request.session[HASH_SESSION_KEY] = session_auth_hash
    else:
        request.session.flush(); user = None
```
核心语义：**当前 SECRET_KEY 验证失败时，必须用 SECRET_KEY_FALLBACKS 中每个旧密钥重算 HMAC（`salted_hmac(..., secret=fallback_secret)`）并 `constant_time_compare`；命中任一则用 `cycle_key()` 续命会话并把 session hash 升级到当前密钥的值，而非 flush 登出**。关键点有三：`_get_session_auth_hash` 必须真的把 `secret` 传给 `salted_hmac`；fallback 生成器必须传 `secret=fallback_secret`；`get_user` 的命中分支用 `any(...)` 且命中即续命。

F2P 测试 `TestGetUser.test_get_user_fallback_secret`：登录后把旧 SECRET_KEY 放进 `SECRET_KEY_FALLBACKS`、换新 SECRET_KEY，断言 `get_user` 仍返回该用户（未登出）且 `session_key` 已变（cycle_key）；随后移除 fallback、只留新 SECRET_KEY，断言会话已用新密钥更新、仍能取到用户。

## 调用链分析

`get_user(request)` → 取 `HASH_SESSION_KEY` → 当前密钥 `user.get_session_auth_hash()` 比对失败 → 遍历 `user.get_session_auth_fallback_hash()`（逐个旧密钥的 HMAC）→ `constant_time_compare` 命中 → `cycle_key()` + 写回当前密钥 hash；否则 `flush()` 登出。`_get_session_auth_hash(secret)` 把 secret 传给 `salted_hmac`。任一环节（漏传 secret、fallback 不传 secret、命中条件反转、守卫破坏、开关默认关闭）都会导致回退失效、用户被登出。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `salted_hmac` 漏传 `secret=secret`，fallback hash 恒等于主 hash |
| B | 🟢 高质量 | 保留 | 命中条件 `any(...)` 前加 `not`，逻辑反转 |
| C | 🟢 高质量 | 保留 | fallback 生成器 `_get_session_auth_hash()` 漏传 `secret=fallback_secret` |
| D | 🟢 高质量 | 保留 | 删除 `hasattr` 守卫并破坏缩进/控制流 |
| E | ➕ 新增 | 新增 | fallback 验证藏到 `SESSION_VERIFY_FALLBACK_SECRETS` 开关后（默认关） |

原始只有 A/B/C/D 四组，缺 E。四组机制各异（漏传 secret / 命中反转 / fallback 漏传 / 守卫破坏），全部保留；补充 E（默认关闭开关）。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：salted_hmac 漏传 secret）
```diff
         return salted_hmac(
             key_salt,
             self.password,
-            secret=secret,
             algorithm="sha256",
         ).hexdigest()
```
**变异语义**：`_get_session_auth_hash` 调 `salted_hmac` 时不传 `secret=secret`，永远用当前 `SECRET_KEY`。于是 `get_session_auth_fallback_hash` 算出的每个 fallback hash 都等于当前密钥的 hash——与已经失配的 session_hash 也不匹配，`any(...)` 恒假 → flush 登出。模拟"用对了带 secret 参数的 API、却在最底层漏传"。保留。

### Group B — 保留（B3 条件反转：命中取反）
```diff
-                    if session_hash and any(
+                    if session_hash and not any(
```
**变异语义**：fallback 命中条件 `any(...)` 前加 `not`。语义颠倒：当某个 fallback 密钥命中（`any` 为真）时 `not` 为假 → 走 else `flush()` 登出（本应续命）；当都不命中时反而 `cycle_key()` 续命（本应登出）。F2P 期望命中续命，实际被登出。保留。

### Group C — 保留（C1 值：fallback 漏传 secret）
```diff
     def get_session_auth_fallback_hash(self):
         for fallback_secret in settings.SECRET_KEY_FALLBACKS:
-            yield self._get_session_auth_hash(secret=fallback_secret)
+            yield self._get_session_auth_hash()
```
**变异语义**：fallback 生成器调 `_get_session_auth_hash()` 不传 `secret=fallback_secret`，用当前密钥而非旧密钥算 hash。与 A 殊途同归（fallback hash 失去旧密钥语义），但出错点在生成器而非底层——`_get_session_auth_hash` 本身仍能正确处理 secret，只是调用方没给。回退验证全失效 → 登出。保留。

### Group D — 保留（D2 状态/控制流：删守卫破坏结构）
```diff
-    if hasattr(user, "get_session_auth_hash"):
         session_auth_hash = user.get_session_auth_hash()
...
-            if hasattr(user, "get_session_auth_hash"):
-                session_hash = request.session.get(HASH_SESSION_KEY)
+            session_hash = request.session.get(HASH_SESSION_KEY)
            （整段去掉一层 if 缩进）
```
**变异语义**：删除 `login` 与 `get_user` 中的 `if hasattr(user, "get_session_auth_hash"):` 守卫并把其下整块外提一层缩进。控制流改变——验证块不再受 hasattr 保护（对无该方法的 user 会 AttributeError），且整体重排引入与 golden 不同的结构，F2P 行为不符。模拟"重构时删掉守卫、破坏了缩进层级"。保留。

### Group E — 新增（E2 隐式→显式开关）
```diff
-                    if session_hash and any(
+                    if getattr(settings, "SESSION_VERIFY_FALLBACK_SECRETS", False) and session_hash and any(
                         constant_time_compare(session_hash, fallback_auth_hash)
                         for fallback_auth_hash in user.get_session_auth_fallback_hash()
                     ):
```
**变异语义**：把 fallback 验证整体藏到 `settings.SESSION_VERIFY_FALLBACK_SECRETS` 开关后（该 setting 默认不存在 → `getattr(..., False)` 为假）。默认情况下命中分支恒不进入 → 直接 `flush()` 登出，还原原 bug。只有显式开启该 setting 才尝试回退密钥。模拟"把回退密钥验证做成可配置、默认却关掉"。新增为 E。

## 新设计 Mutation 说明

原始仅 A/B/C/D 四组，缺第五组。四组机制互异：A（底层 `salted_hmac` 漏传 secret）、B（命中条件 `not` 反转）、C（fallback 生成器漏传 secret）、D（删 hasattr 守卫并破坏缩进/控制流），全部保留。补充 E（`SESSION_VERIFY_FALLBACK_SECRETS` 默认关闭开关）。五组覆盖"底层漏传 / 命中反转 / 生成器漏传 / 守卫破坏 / 默认关闭开关"五个角度，分别作用于 `salted_hmac` 调用、`get_user` 命中分支、fallback 生成器、控制流结构、特性开关五个环节——全部令 fallback 密钥验证失效、用户被登出。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
