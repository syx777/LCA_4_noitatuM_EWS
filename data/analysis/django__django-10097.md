# django__django-10097

## 问题背景

RFC 1738 section 3.1 规定 URL 中用户名和密码字段里的 `:`、`@`、`/` 必须经过百分号编码（percent-encoding）。然而 Django 的 `URLValidator` 使用的正则 `(?:\S+(?::\S*)?@)?` 只排除空白字符，允许了 `:`、`@`、`/` 等非法字符直接出现在用户名/密码部分。

Golden patch 将用户名字符类从 `\S+`（任意非空白）改为 `[^\s:@/]+`（排除空白、冒号、at符号、斜杠），将密码字符类从 `\S*` 改为 `[^\s:@/]*`（同样排除这三类字符）。这使得 `http://foo@bar@example.com`、`http://foo/bar@example.com`、`http://foo:bar:baz@example.com` 等 URL 被正确判定为无效。

同时，`tests/validators/valid_urls.txt` 中原来的一条包含多重冒号的有效URL `http://-.~_!$&'()*+,;=:%40:80%2f::::::@example.com` 被简化为 `http://-.~_!$&'()*+,;=%40:80%2f@example.com`（移除了多余的冒号序列）。

## Golden Patch 语义分析

**修复核心**：正则从"用户名和密码可以是任意非空白字符序列"改为"用户名和密码不能包含 `:`、`@`、`/`"。

- **为什么 `\S+` 是错误的**：`\S+` 包含 `@` 字符，导致正则引擎会贪婪地匹配 `foo@bar`，把整个 `foo@bar` 当作用户名，后续再匹配一个合法的 `@host` 结构，从而让 `http://foo@bar@example.com` 通过验证。
- **为什么 `[^\s:@/]+` 是正确的**：排除 `@` 后，用户名段遇到 `@` 就停止，防止多重 `@` 歧义；排除 `:` 防止 `foo:bar:baz` 被解析为用户名含冒号；排除 `/` 防止 `foo/bar@example.com` 被解析为含路径的用户名。
- **密码部分** `[^\s:@/]*`：排除 `@` 和 `/` 防止密码中混入 host 部分，排除 `:` 防止多重冒号歧义（`foo:bar:baz`）。

## 调用链分析

```
URLValidator.__call__(value)
    └─ scheme = value.split('://')[0].lower()  # 提取 scheme
    └─ scheme in self.schemes  # scheme 验证
    └─ super().__call__(value)  # RegexValidator.__call__
            └─ self.regex.search(str(value))  # 正则匹配（包含 user:pass 部分）
    └─ [except ValidationError: IDN fallback]
            └─ urlsplit(value) → scheme, netloc, path, query, fragment
            └─ netloc.encode('idna').decode('ascii')
            └─ url = urlunsplit(...)
            └─ super().__call__(url)  # 重新验证 ACE 编码后的 URL
    └─ [else: IPv6 验证]
            └─ re.search(r'^\[(.+)\](?::\d{2,5})?$', urlsplit(value).netloc)
            └─ validate_ipv6_address(potential_ip)
    └─ len(urlsplit(value).netloc) > 253  # netloc 长度检查
```

**关键数据流**：`value`（原始 URL 字符串）→ `self.regex`（含 user:pass 正则）匹配 → 若失败走 IDN 路径 → 最终 netloc 长度检查。

**修改影响范围**：`URLValidator.regex` 类属性，通过 `RegexValidator.__call__` 被直接使用。IDN fallback 路径会对 ACE 编码后的 URL 再次调用 `super().__call__(url)`，因此同样受 regex 修改影响。

## 替换决策总览

本实例为 Path B（新实例），直接为 A/B/C/D/E 五个策略组各设计一个高质量 mutation。

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | A1（API 契约变更） | 新设计 | 密码部分允许 `/`，使含斜杠密码的 URL 绕过验证 |
| B | B3（条件逻辑翻转） | 新设计 | 用户名允许 `:`，使多重冒号的 URL 绕过验证 |
| C | C1（类型转换破坏） | 新设计 | IDN fallback 路径跳过二次验证，IDN URL 绕过 regex 检查 |
| D | D1（状态初始化破坏） | 新设计 | netloc 长度检查用 hostname 替代 netloc，跳过 userinfo 长度 |
| E | E1（断言期望更新） | 新设计 | 用户名和密码均额外排除 `%`，使含百分号编码的合法 URL 被错误拒绝 |

## 各组 Mutation 分析

### Group A — 替换（新设计）
**原 mutation**：无（Path B 新实例）

**分类**：新设计 — A1（修改 API 规范/正则契约）

**设计思路**：Golden patch 同时对用户名和密码部分都排除了 `:`、`@`、`/`。Mutation A 仅在密码部分"忘记"排除 `/`，保持用户名限制正确，只放开密码部分的 `/`。

- `http://foo:bar/baz@example.com` — 密码 `bar/baz` 含 `/`，golden patch 后应为无效，mutation A 后变为有效
- `http://foo@bar@example.com` — 用户名含 `@`，两者均拒绝（用户名限制未改）
- `http://foo:bar:baz@example.com` — 用户名含 `:` 仍被拒绝
- `http://-.~_!$&'()*+,;=%40:80%2f@example.com` — 用户名和密码不含 `/`，仍通过

**最终 mutation**：
```diff
diff --git a/django/core/validators.py b/django/core/validators.py
index c1c9cd1c87..ccc1a4d0fb 100644
--- a/django/core/validators.py
+++ b/django/core/validators.py
@@ -94,7 +94,7 @@ class URLValidator(RegexValidator):
 
     regex = _lazy_re_compile(
         r'^(?:[a-z0-9\.\-\+]*)://'  # scheme is validated separately
-        r'(?:[^\s:@/]+(?::[^\s:@/]*)?@)?'  # user:pass authentication
+        r'(?:[^\s:@/]+(?::[^\s:@]*)?@)?'  # user:pass authentication
         r'(?:' + ipv4_re + '|' + ipv6_re + '|' + host_re + ')'
         r'(?::\d{2,5})?'  # port
         r'(?:[/?#][^\s]*)?'  # resource path
```

**变异语义**：密码字符类从 `[^\s:@/]` 改为 `[^\s:@]`，仅去掉了对 `/` 的排除。密码中的 `/` 不再被禁止，导致 `http://foo:bar/baz@example.com` 被错误地判定为有效。该变化在代码审查中极难发现——两行正则只有一个字符 `/` 的差异，且改动位于复杂字符类内部。所有不含斜杠密码的测试均通过，只有专门测试"密码含斜杠"场景的 F2P 测试会失败。

---

### Group B — 替换（新设计）
**原 mutation**：无（Path B 新实例）

**分类**：新设计 — B3（条件逻辑部分翻转）

**设计思路**：Golden patch 在用户名部分排除了 `:`，防止 `foo:bar:baz` 中 `foo:bar` 被作为 `user:pass` 解析（多余的冒号让用户名含冒号）。Mutation B "忘记"在用户名字符类中排除 `:`，保持密码限制正确。

- `http://foo:bar:baz@example.com` — 用户名可以是 `foo:bar`（含冒号），密码为 `baz`，变为有效
- `http://foo@bar@example.com` — 用户名含 `@`，仍被拒绝
- `http://foo/bar@example.com` — 用户名含 `/`，仍被拒绝

**最终 mutation**：
```diff
diff --git a/django/core/validators.py b/django/core/validators.py
index c1c9cd1c87..7c61f5a4ce 100644
--- a/django/core/validators.py
+++ b/django/core/validators.py
@@ -94,7 +94,7 @@ class URLValidator(RegexValidator):
 
     regex = _lazy_re_compile(
         r'^(?:[a-z0-9\.\-\+]*)://'  # scheme is validated separately
-        r'(?:[^\s:@/]+(?::[^\s:@/]*)?@)?'  # user:pass authentication
+        r'(?:[^\s@/]+(?::[^\s:@/]*)?@)?'  # user:pass authentication
         r'(?:' + ipv4_re + '|' + ipv6_re + '|' + host_re + ')'
         r'(?::\d{2,5})?'  # port
         r'(?:[/?#][^\s]*)?'  # resource path
```

**变异语义**：用户名字符类从 `[^\s:@/]` 改为 `[^\s@/]`，去掉了对 `:` 的排除。用户名中可以含冒号，导致 `foo:bar:baz` 被解析为用户名=`foo:bar`、密码=`baz`，从而通过验证。该变化极难发现——用户名和密码两处字符类仅有一字符差异，且逻辑上"用户名允许冒号似乎无害"容易被误认为合理。

---

### Group C — 替换（新设计）
**原 mutation**：无（Path B 新实例）

**分类**：新设计 — C1（破坏类型转换/验证逻辑）

**设计思路**：`__call__` 在 IDN fallback 路径中先将 netloc encode 为 ASCII（ACE），然后用 `super().__call__(url)` 重新验证完整 URL。Mutation 将这最后的重新验证替换为 `return`，使 IDN URL 在 ACE 编码成功后完全绕过 regex 验证。

**影响**：
- 任何包含 Unicode 域名的 URL（如 `http://foo@bar@مثال.إختبار`）在 IDN 编码路径中会跳过 regex 验证，即使含有非法的 `@` 字符也能通过
- 正常 ASCII URL 走主路径（`try: super().__call__(value)`），不受影响
- 但 IDN URL（unicode 域名）如果用户名/密码含非法字符，将错误地被判定为有效

**最终 mutation**：
```diff
diff --git a/django/core/validators.py b/django/core/validators.py
index c1c9cd1c87..e1cae85047 100644
--- a/django/core/validators.py
+++ b/django/core/validators.py
@@ -128,7 +128,7 @@ class URLValidator(RegexValidator):
                 except UnicodeError:  # invalid domain part
                     raise e
                 url = urlunsplit((scheme, netloc, path, query, fragment))
-                super().__call__(url)
+                return  # skip re-validation for IDN domains
             else:
                 raise
         else:
```

**变异语义**：IDN fallback 路径跳过对 ACE 编码后 URL 的正则验证。对普通 ASCII URL，main path 正常工作；对 Unicode 域名 URL，IDN 编码成功即被视为合法，绕过所有字符合法性检查。难以发现：只有在测试 unicode 域名且 user:pass 含非法字符时才会暴露。

---

### Group D — 替换（新设计）
**原 mutation**：无（Path B 新实例）

**分类**：新设计 — D1（破坏状态/契约）

**设计思路**：`__call__` 末尾通过 `len(urlsplit(value).netloc) > 253` 检查 host 长度。`netloc` 包含 `userinfo@host:port` 全部内容，而 `hostname` 只包含纯主机名部分。将 `netloc` 改为 `hostname`，相当于把 userinfo（`foo@`）和端口（`:8080`）的长度排除在外，导致对含长用户名或长端口的 URL 放宽了长度限制，让本该因总长度超标而无效的 URL 通过。

**最终 mutation**：
```diff
diff --git a/django/core/validators.py b/django/core/validators.py
index c1c9cd1c87..03ba816e58 100644
--- a/django/core/validators.py
+++ b/django/core/validators.py
@@ -145,7 +145,7 @@ class URLValidator(RegexValidator):
         # section 3.1. It's defined to be 255 bytes or less, but this includes
         # one byte for the length of the name and one byte for the trailing dot
         # that's used to indicate absolute names in DNS.
-        if len(urlsplit(value).netloc) > 253:
+        if len(urlsplit(value).hostname or '') > 253:
             raise ValidationError(self.message, code=self.code)
```

**变异语义**：将 netloc 长度检查（包含 userinfo+host+port）改为仅检查 hostname 长度。对典型不含用户名的 URL（大多数测试），hostname 和 netloc 相同，行为一致；只有在含用户名或端口的 URL 中，netloc 比 hostname 长，使本该因总长度超标的 URL 绕过检查。难以发现：`hostname` 与 `netloc` 拼写相似，且代码注释提到"255 bytes"但实际检查的是整个 netloc——替换后逻辑"看上去更精确"。

---

### Group E — 替换（新设计）
**原 mutation**：无（Path B 新实例）

**分类**：新设计 — E1（断言期望更新）

**设计思路**：Golden patch 同时将 `tests/validators/valid_urls.txt` 中的 `http://-.~_!$&'()*+,;=:%40:80%2f::::::@example.com` 改为 `http://-.~_!$&'()*+,;=%40:80%2f@example.com`（保留百分号编码的 `%40` 和 `%2f`）。Mutation E 在用户名和密码的字符类中额外排除 `%`，导致含 `%40`（编码的 `@`）的用户名/密码也被拒绝，使合法的百分号编码 URL 变为无效，与 `valid_urls.txt` 中的期望冲突。

**最终 mutation**：
```diff
diff --git a/django/core/validators.py b/django/core/validators.py
index c1c9cd1c87..3dba94a5b4 100644
--- a/django/core/validators.py
+++ b/django/core/validators.py
@@ -94,7 +94,7 @@ class URLValidator(RegexValidator):
 
     regex = _lazy_re_compile(
         r'^(?:[a-z0-9\.\-\+]*)://'  # scheme is validated separately
-        r'(?:[^\s:@/]+(?::[^\s:@/]*)?@)?'  # user:pass authentication
+        r'(?:[^\s:@/%]+(?::[^\s:@/%]*)?@)?'  # user:pass authentication
         r'(?:' + ipv4_re + '|' + ipv6_re + '|' + host_re + ')'
         r'(?::\d{2,5})?'  # port
         r'(?:[/?#][^\s]*)?'  # resource path
```

**变异语义**：用户名和密码字符类额外排除 `%`，导致百分号编码的凭证（如 `%40`、`%2f`）被错误拒绝。`http://-.~_!$&'()*+,;=%40:80%2f@example.com`（valid_urls.txt 中的合法 URL）会因用户名含 `%` 而被判定为无效，违背 valid_urls.txt 的期望。难以发现：乍看"禁止 `%` 在凭证中"似乎更严格/更安全，但实际上违反了"百分号编码字符应被接受"的规范。

---

## 新设计 Mutation 说明

所有5个 mutation 均为新设计（Path B 新实例），基于以下代码分析：

1. **核心修改位置**：`URLValidator.regex` 中的 user:pass 正则 `r'(?:[^\s:@/]+(?::[^\s:@/]*)?@)?'`，包含用户名字符类（`[^\s:@/]+`）和密码字符类（`[^\s:@/]*`）两个独立部分。
2. **调用链影响**：`__call__` 方法通过 IDN fallback 对 ACE 编码后的 URL 重新调用 `super().__call__(url)`，提供了额外的变异切入点（Group C）。
3. **额外验证逻辑**：`__call__` 末尾的 netloc 长度检查独立于 regex 之外，提供了 Group D 的切入点。
4. **测试期望**：`valid_urls.txt` 明确期望含百分号编码的 URL 有效，Group E 针对此期望设计了恰好相反的行为。
5. **部分修复模式**：A/B 两组均模拟"只修复了一半"的真实开发者错误——A 组忘记在密码部分排除 `/`，B 组忘记在用户名部分排除 `:`。
