# django__django-11532

## 问题背景

当计算机的主机名设置为 Unicode（如中文字符 "正宗" 或 "漢字"），且邮件编码设置为非 Unicode 编码（如 `iso-8859-1`）时，Django 的邮件系统在生成 Message-ID 时崩溃。

具体原因：`CachedDnsName.get_fqdn()` 返回的是原始（未经 punycode 编码）的主机名，而 `make_msgid(domain=DNS_NAME)` 会将该主机名嵌入 Message-ID 头部。非 ASCII 的主机名无法用 iso-8859-1 编码，导致 `UnicodeEncodeError`。

## Golden Patch 语义分析

Golden patch 的核心工作：

1. **在 `django/utils/encoding.py` 新增 `punycode(domain)` 函数**：统一封装了 IDNA 编码逻辑，使所有调用方共享同一实现。
2. **修复 `CachedDnsName.get_fqdn()`（最关键行为变更）**：从 `self._fqdn = socket.getfqdn()` 改为 `self._fqdn = punycode(socket.getfqdn())`，确保缓存的主机名始终是 ASCII 安全的 punycode 形式。
3. **重构 `sanitize_address()` 中的域名处理**（纯重构，语义不变）：用 `domain = punycode(domain)` 替代原有的 try/except inline。
4. **重构 `validators.py` 和 `html.py` 中的 IDN 处理**（纯重构）：同样替换为统一的 `punycode()` 调用。

唯一真正改变行为的是第 2 点：`get_fqdn()` 现在存储的是经过 punycode 编码的主机名，而不是原始 Unicode 主机名。

## 调用链分析

```
EmailMessage.message()
    └── make_msgid(domain=DNS_NAME)
            └── str(DNS_NAME)
                    └── CachedDnsName.__str__()
                            └── CachedDnsName.get_fqdn()
                                    └── punycode(socket.getfqdn())
```

另外，`sanitize_address()` 有独立的域名处理路径：
```
sanitize_address(addr, encoding)
    └── punycode(domain)  # 处理邮件地址中的非 ASCII 域名
```

`punycode()` 函数被以下位置调用：
- `utils.py`: `get_fqdn()` ← **唯一行为变更点**
- `message.py`: `sanitize_address()`（重构）
- `validators.py`: `URLValidator.__call__()`、`EmailValidator.__call__()`（重构）
- `html.py`: `smart_urlquote()`、`urlize()`（重构）

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | encoding.py 中添加 `_convert=False` 参数，与 Group E 功能等价冗余，且都在 encoding.py 中 |
| B | 缺失 | 新设计 | mutations.jsonl 中不存在 Group B |
| C | 缺失 | 新设计 | mutations.jsonl 中不存在 Group C |
| D | 🔴 必须替换 | 替换 | `__str__` 添加冗余逻辑不自然；核心 bug 是移除 punycode，但表达方式有人工痕迹 |
| E | 🔴 必须替换 | 替换 | encoding.py 中添加 `encode_to_ascii=False` 参数，与 Group A 功能等价冗余 |

语义浅层共 0 个；所有现有 mutation 均为必须替换类型（A、D、E）+ 新设计（B、C）。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation（utils.py + encoding.py）**：
```diff
# encoding.py: 添加 _convert=False 参数，使 punycode() 默认返回原始值
-def punycode(domain):
+def punycode(domain, _convert=False):
     """Return the Punycode of the given domain if it's non-ASCII."""
+    if not _convert:
+        return domain
     return domain.encode('idna').decode('ascii')
# utils.py: 也做了冗余修改
```
**分类**：🔴 必须替换
**理由**：在 encoding.py 中添加 `_convert=False` 默认参数，使所有调用点（5+ 处）的 `punycode()` 均变成无操作。这与 Group E 的 `encode_to_ascii=False` 方案功能等价，且两者都集中在同一个文件中，属于直接功能冗余。

**最终 mutation（A2 — Break API Convention）**：
```diff
diff --git a/django/core/mail/utils.py b/django/core/mail/utils.py
--- a/django/core/mail/utils.py
+++ b/django/core/mail/utils.py
@@ -11,7 +11,9 @@ from django.utils.encoding import punycode
 # seconds, which slows down the restart of the server.
 class CachedDnsName:
     def __str__(self):
-        return self.get_fqdn()
+        if not hasattr(self, '_fqdn'):
+            self._fqdn = socket.getfqdn()
+        return self._fqdn
 
     def get_fqdn(self):
         if not hasattr(self, '_fqdn'):
```
**变异语义**：`__str__` 方法引入了独立的缓存逻辑，直接调用 `socket.getfqdn()` 但**不经过 `punycode()` 转换**。`__str__` 与 `get_fqdn()` 共享 `_fqdn` 属性，但 `__str__` 先设置时会存入原始 Unicode 主机名。`make_msgid(domain=DNS_NAME)` 通过 `str(DNS_NAME)` 调用 `__str__`，获得未编码的 Unicode 主机名，用非 Unicode 编码时崩溃。**难以发现原因**：开发者可能认为这是"优化"——避免通过方法调用直接访问缓存，看起来合理且功能相近，但打破了 `__str__` 应委托给 `get_fqdn()` 的约定。

---

### Group B — 新设计

**原 mutation**：（不存在）

**分类**：新设计（B3 — Invert Boolean Logic）

**最终 mutation**：
```diff
diff --git a/django/core/mail/message.py b/django/core/mail/message.py
--- a/django/core/mail/message.py
+++ b/django/core/mail/message.py
@@ -102,7 +102,12 @@ def sanitize_address(addr, encoding):
         localpart.encode('ascii')
     except UnicodeEncodeError:
         localpart = Header(localpart, encoding).encode()
-    domain = punycode(domain)
+    try:
+        domain.encode('ascii')
+    except UnicodeEncodeError:
+        pass
+    else:
+        domain = domain.encode('idna').decode('ascii')
 
     parsed_address = Address(nm, username=localpart, domain=domain)
     return str(parsed_address)
```
**变异语义**：反转了域名编码的条件逻辑：原逻辑（golden patch）对所有域名调用 `punycode()`；mutation 将 try/except 的语义颠倒为"仅对 ASCII 域名做 IDNA 编码（实为无操作），非 ASCII 域名保持原样"。非 ASCII 域名（如 `éxample.com`）将不会被转换为 punycode（`xn--xample-9ua.com`），`test_sanitize_address` 中的 IDN 测试用例会失败。**难以发现原因**：try/except/else 的流程方向容易混淆，看上去开发者"似乎在处理 ASCII/非 ASCII 两种情况"，但实际上逻辑颠倒了。

---

### Group C — 新设计

**原 mutation**：（不存在）

**分类**：新设计（C3 — Confuse Text vs. Bytes Encoding）

**最终 mutation**：
```diff
diff --git a/django/utils/encoding.py b/django/utils/encoding.py
--- a/django/utils/encoding.py
+++ b/django/utils/encoding.py
@@ -220,7 +220,11 @@ def escape_uri_path(path):
 
 def punycode(domain):
     """Return the Punycode of the given domain if it's non-ASCII."""
-    return domain.encode('idna').decode('ascii')
+    try:
+        domain.encode('ascii')
+        return domain
+    except UnicodeEncodeError:
+        return domain.encode('utf-8').decode('utf-8')
 
 
 def repercent_broken_unicode(path):
```
**变异语义**：将 `'idna'` 编码混淆为 `'utf-8'` 编码。对非 ASCII 域名（如 `'漢字'`、`'éxample.com'`），原本应调用 `encode('idna').decode('ascii')` 得到 punycode 形式，现在改为 `encode('utf-8').decode('utf-8')` 完全是无操作——返回原始 Unicode 字符串。这使得所有调用 `punycode()` 的地方对非 ASCII 域名均返回错误结果。影响 `get_fqdn()`、`sanitize_address()`、validator 及 html 中的所有 IDN 转换。**难以发现原因**：`utf-8` 编码是 Python 中最常用的编码，开发者可能误认为它适用于"编码"域名；而 `idna` 是专门的域名编码，不如 `utf-8` 直观。

---

### Group D — 替换

**原 mutation（utils.py）**：
```diff
# __str__ 添加独立缓存（不调 punycode），get_fqdn() 也移除 punycode
     def __str__(self):
-        return self.get_fqdn()
+        if not hasattr(self, '_fqdn'):
+            self._fqdn = socket.getfqdn()
+        return self._fqdn

     def get_fqdn(self):
         if not hasattr(self, '_fqdn'):
-            self._fqdn = punycode(socket.getfqdn())
+            self._fqdn = socket.getfqdn()
```
**分类**：🔴 必须替换
**理由**：`__str__` 中添加的独立缓存与 `get_fqdn()` 逻辑完全重复，且两者都去掉了 punycode——冗余修改暴露了人工设计痕迹。真正的 bug 只需修改 `get_fqdn()` 中的一行，而 `__str__` 的冗余逻辑是多余的。

**最终 mutation（D4 — Break Environment or Resource Handling）**：
```diff
diff --git a/django/core/mail/utils.py b/django/core/mail/utils.py
--- a/django/core/mail/utils.py
+++ b/django/core/mail/utils.py
@@ -15,7 +15,7 @@ class CachedDnsName:
 
     def get_fqdn(self):
         if not hasattr(self, '_fqdn'):
-            self._fqdn = punycode(socket.getfqdn())
+            self._fqdn = punycode(socket.gethostname())
         return self._fqdn
```
**变异语义**：将 `socket.getfqdn()`（返回完整的 FQDN）替换为 `socket.gethostname()`（仅返回短主机名）。测试用例 `test_non_ascii_dns_non_unicode_email` mock 的是 `socket.getfqdn` 而非 `socket.gethostname`，因此 mock 不生效，`get_fqdn()` 返回真实主机名（ASCII），Message-ID 中不包含 `xn--p8s937b`，断言失败。**难以发现原因**：`gethostname` 和 `getfqdn` 都返回主机名，在 ASCII 环境中行为相同，差异仅在非 ASCII 主机名或 mock 场景下暴露；类名 `CachedDnsName` 语义上用于获取 DNS 名（FQDN），但 `gethostname` 在普通代码审查中看起来同样合理。

---

### Group E — 替换

**原 mutation（encoding.py）**：
```diff
-def punycode(domain):
+def punycode(domain, encode_to_ascii=False):
     """Return the Punycode of the given domain if it's non-ASCII."""
-    return domain.encode('idna').decode('ascii')
+    if encode_to_ascii:
+        return domain.encode('idna').decode('ascii')
+    return domain
```
**分类**：🔴 必须替换
**理由**：与 Group A 的 `_convert=False` 方案功能完全等价——都是在 `punycode()` 函数上添加默认为 False 的参数，使所有现有调用变成无操作，等同于完全还原 golden patch。两者集中在同一文件（encoding.py），互为冗余。

**最终 mutation（E2 — Implicit → Explicit Parameter）**：
```diff
diff --git a/django/core/validators.py b/django/core/validators.py
--- a/django/core/validators.py
+++ b/django/core/validators.py
@@ -178,6 +178,7 @@ class EmailValidator:
     domain_whitelist = ['localhost']
+    idn_enabled = False
 
@@ -199,13 +200,14 @@ class EmailValidator:
         if (domain_part not in self.domain_whitelist and
                 not self.validate_domain_part(domain_part)):
             # Try for possible IDN domain-part
-            try:
-                domain_part = punycode(domain_part)
-            except UnicodeError:
-                pass
-            else:
-                if self.validate_domain_part(domain_part):
-                    return
+            if self.idn_enabled:
+                try:
+                    domain_part = punycode(domain_part)
+                except UnicodeError:
+                    pass
+                else:
+                    if self.validate_domain_part(domain_part):
+                        return
             raise ValidationError(self.message, code=self.code)
```
**变异语义**：为 `EmailValidator` 添加类属性 `idn_enabled = False`，将 IDN（国际域名）邮件验证逻辑设为默认关闭。原来隐式支持 IDN 邮件地址（如 `user@漢字.com`），现在变为必须显式开启。所有使用默认 `validate_email` 函数的 IDN 邮件验证测试将失败，因为 `validate_email = EmailValidator()` 创建实例时 `idn_enabled=False`，IDN 域名验证路径完全绕过。**难以发现原因**：添加功能开关是常见设计模式（符合"保守原则"），代码结构清晰，但默认值 `False` 悄悄改变了原有行为。

## 新设计 Mutation 说明

### Group B（B3，message.py）

基于对 `sanitize_address()` 调用链的分析：golden patch 将 try/except 替换为无条件的 `domain = punycode(domain)`。mutation 以"更保守的方式"重写：用 try/except/else 结构，但逻辑颠倒——只对 ASCII 域名做 IDNA 转换（实为无操作），对非 ASCII 域名直接跳过（应该转换的场景反而不处理）。模拟了开发者在理解 try/except/else 流程时出现方向性错误的真实失误。

### Group C（C3，encoding.py）

基于对 `punycode()` 函数的分析：该函数的核心是 `encode('idna')` 将 Unicode 域名转为 ASCII 兼容编码（ACE/punycode）格式。mutation 将 `'idna'` 替换为 `'utf-8'`，而 `encode('utf-8').decode('utf-8')` 对字符串是纯无操作。模拟了开发者将"字节编解码"与"IDNA 域名转换"混淆的错误——`utf-8` 是处理 Unicode 最常见的编码，而 `idna` 是专门的域名编码规范，两者容易混淆。
