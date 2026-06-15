# django__django-11099

## 问题背景

`ASCIIUsernameValidator` 和 `UnicodeUsernameValidator` 均使用正则表达式 `r'^[\w.@+-]+$'` 来验证用户名。Python 正则中 `$` 匹配行末，但有一个鲜为人知的特性：`$` 也能匹配字符串末尾的换行符之前的位置。因此，`"trailingnewline\n"` 这样的用户名会被两个验证器接受，违反了"只允许字母数字和特定符号"的设计意图。

Golden patch 将两个验证器的正则改为 `r'^[\w.@+-]+\Z'`，其中 `\Z` 严格匹配字符串绝对末尾（不允许末尾换行），从而修复了这一漏洞。

## Golden Patch 语义分析

修复核心：将两个验证器中的 `$` 改为 `\Z`。

- `$` 的语义：匹配行末，在默认（非 MULTILINE）模式下匹配字符串末尾或末尾换行符之前
- `\Z` 的语义：严格匹配字符串的绝对末尾，不受换行符影响

修复逻辑清晰：不改变任何其他属性，只收紧末尾锚点的匹配规则。

## 调用链分析

```
用户名验证流程：
  UserModel.clean_fields() / form validation
    → ASCIIUsernameValidator.__call__(value)  [继承自 RegexValidator]
        → self.regex.search(str(value))        [_lazy_re_compile 编译的 SimpleLazyObject]
        → if not match: raise ValidationError
    → UnicodeUsernameValidator.__call__(value) [同上]
```

被修改的是两个验证器的类属性 `regex`。`RegexValidator.__init__` 在实例化时执行 `self.regex = _lazy_re_compile(self.regex, self.flags)`，将字符串正则编译为懒加载的 compiled regex 对象。`__call__` 方法使用 `search()` 进行匹配，配合 `^` 和 `\Z` 锚点时等价于全串匹配。

关键属性对比（base_commit 状态）：
- `ASCIIUsernameValidator`: `regex = r'^[\w.@+-]+$'`, `flags = re.ASCII`
- `UnicodeUsernameValidator`: `regex = r'^[\w.@+-]+$'`, `flags = 0`

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 新设计 | 新增 | mutations.jsonl 中不存在，需全新设计 |
| B | 新设计 | 新增 | mutations.jsonl 中不存在，需全新设计 |
| C | 新设计 | 新增 | mutations.jsonl 中不存在，需全新设计 |
| D | 🟢 保留 | 保留 | 删除 flags = re.ASCII，修改不同属性，使 ASCII 验证器接受 Unicode 字符 |
| E | 🔴 必须替换 | 替换 | 功能等价冗余：通过复杂 __init__ 实质上恢复原始 $ 行为，与直接还原 patch 效果相同 |

## 各组 Mutation 分析

### Group A — 新增（只修复 ASCII，遗漏 Unicode）

**原 mutation**：不存在

**分类**：新设计

**设计思路**：模拟开发者只修复了 `ASCIIUsernameValidator` 的 `$` → `\Z`，而忘记同步修改 `UnicodeUsernameValidator`。这是实际开发中常见的"部分修复"错误，代码审查者可能不会注意到只有 ASCII 验证器被修改了。

**最终 mutation**：
```diff
diff --git a/django/contrib/auth/validators.py b/django/contrib/auth/validators.py
index 9345c5cb0b..9096ab0314 100644
--- a/django/contrib/auth/validators.py
+++ b/django/contrib/auth/validators.py
@@ -17,7 +17,7 @@ class ASCIIUsernameValidator(validators.RegexValidator):
 
 @deconstructible
 class UnicodeUsernameValidator(validators.RegexValidator):
-    regex = r'^[\w.@+-]+\Z'
+    regex = r'^[\w.@+-]+$'
     message = _(
         'Enter a valid username. This value may contain only letters, '
         'numbers, and @/./+/-/_ characters.'
```

**变异语义**：`UnicodeUsernameValidator` 仍然使用 `$` 锚点，在 F2P 测试中 `'trailingnewline\u000A'` 仍被接受（不抛 ValidationError），导致 `test_unicode_validator` 失败。`ASCIIUsernameValidator` 正确修复，`test_ascii_validator` 通过。难以发现：两个类并排定义，代码审查时容易只注意 ASCII 已修复。

---

### Group B — 新增（UnicodeUsernameValidator 误用 ASCII 标志）

**原 mutation**：不存在

**分类**：新设计

**设计思路**：将 `UnicodeUsernameValidator` 的 `flags = 0` 改为 `flags = re.ASCII`。这使得 `\w` 只匹配 ASCII 字符，导致合法的 Unicode 用户名（如 `'René'`、`'أحمد'`）被错误拒绝。模拟了开发者在修改时错误地将 ASCII 标志复制到 Unicode 验证器的场景。

**最终 mutation**：
```diff
diff --git a/django/contrib/auth/validators.py b/django/contrib/auth/validators.py
index 9345c5cb0b..0ae7504bad 100644
--- a/django/contrib/auth/validators.py
+++ b/django/contrib/auth/validators.py
@@ -22,4 +22,4 @@ class UnicodeUsernameValidator(validators.RegexValidator):
         'Enter a valid username. This value may contain only letters, '
         'numbers, and @/./+/-/_ characters.'
     )
-    flags = 0
+    flags = re.ASCII
```

**变异语义**：`UnicodeUsernameValidator` 在 `re.ASCII` 模式下 `\w` 只匹配 `[a-zA-Z0-9_]`，Unicode 字母不再匹配。`test_unicode_validator` 中 `valid_usernames = ['joe', 'René', 'ᴮᴵᴳᴮᴵᴿᴰ', 'أحمد']` 里的后三个会抛 ValidationError，测试失败。难以发现：`flags` 属性只有一处微小变化（`0` → `re.ASCII`），逻辑上看似合理（维护者可能认为"统一用 ASCII 模式更安全"）。

---

### Group C — 新增（字符集扩展允许空白字符）

**原 mutation**：不存在

**分类**：新设计

**设计思路**：将 `UnicodeUsernameValidator.regex` 中的字符类从 `[\w.@+-]` 改为 `[\w.@+-\s]`，添加 `\s`（匹配任何空白字符，包括空格和换行符）。这既允许了含空格的用户名（如 `"عبد ال"`），也允许了含换行符的用户名（如 `'trailingnewline\u000A'`）。

**最终 mutation**：
```diff
diff --git a/django/contrib/auth/validators.py b/django/contrib/auth/validators.py
index 9345c5cb0b..64f4a1e3bd 100644
--- a/django/contrib/auth/validators.py
+++ b/django/contrib/auth/validators.py
@@ -17,7 +17,7 @@ class ASCIIUsernameValidator(validators.RegexValidator):
 
 @deconstructible
 class UnicodeUsernameValidator(validators.RegexValidator):
-    regex = r'^[\w.@+-]+\Z'
+    regex = r'^[\w.@+-\s]+\Z'
     message = _(
         'Enter a valid username. This value may contain only letters, '
         'numbers, and @/./+/-/_ characters.\'
```

**变异语义**：添加 `\s` 后，包含空格的无效用户名 `"عبد ال"`、`"zerowidth\u200Bspace"` 等部分会被接受（`\u200B` 属于 `\s` 范畴），且 `'trailingnewline\u000A'` 中的 `\n` 也是 `\s`，导致 F2P 测试失败。难以发现：`\s` 在字符类中看似无关紧要的补充，开发者可能以为只是允许"友好的空格用户名"。

---

### Group D — 保留（删除 ASCIIUsernameValidator.flags = re.ASCII）

**原 mutation**：
```diff
diff --git a/django/contrib/auth/validators.py b/django/contrib/auth/validators.py
index 9345c5cb0b..586d71ef1d 100644
--- a/django/contrib/auth/validators.py
+++ b/django/contrib/auth/validators.py
@@ -12,7 +12,6 @@ class ASCIIUsernameValidator(validators.RegexValidator):
         'Enter a valid username. This value may contain only English letters, '
         'numbers, and @/./+/-/_ characters.'
     )
-    flags = re.ASCII
 
 
 @deconstructible
```

**分类**：🟢 保留

**理由**：修改的是 `flags` 属性（非 golden patch 修改的 `regex` 属性），位于不同的字段。删除 `re.ASCII` 后，`\w` 匹配 Unicode 字符，`ASCIIUsernameValidator` 会接受 `'Éric'`、`'أحمد'` 等原本无效的 Unicode 用户名，导致 `test_ascii_validator` 失败。这模拟了开发者误删 `flags` 行的场景，是对验证器语义契约的破坏。

**最终 mutation**（与原相同）：
```diff
diff --git a/django/contrib/auth/validators.py b/django/contrib/auth/validators.py
index 9345c5cb0b..586d71ef1d 100644
--- a/django/contrib/auth/validators.py
+++ b/django/contrib/auth/validators.py
@@ -12,7 +12,6 @@ class ASCIIUsernameValidator(validators.RegexValidator):
         'Enter a valid username. This value may contain only English letters, '
         'numbers, and @/./+/-/_ characters.'
     )
-    flags = re.ASCII
 
 
 @deconstructible
```

**变异语义**：`ASCIIUsernameValidator` 失去 ASCII 限制，接受 Unicode 用户名。简单测试（纯 ASCII 用户名）均能通过，只在测试非 ASCII 输入时失败。

---

### Group E — 替换（接口契约反转：inverse_match）

**原 mutation**（🔴 必须替换）：
```diff
diff --git a/django/contrib/auth/validators.py b/django/contrib/auth/validators.py
index 9345c5cb0b..52e7d04401 100644
--- a/django/contrib/auth/validators.py
+++ b/django/contrib/auth/validators.py
@@ -7,7 +7,14 @@ from django.utils.translation import gettext_lazy as _
 
 @deconstructible
 class ASCIIUsernameValidator(validators.RegexValidator):
-    regex = r'^[\w.@+-]+\Z'
+    def __init__(self, strict=False):
+        self.strict = strict
+        if strict:
+            self.regex = r'^[\w.@+-]+\Z'
+        else:
+            self.regex = r'^[\w.@+-]+$'
+        super().__init__()
+    
     message = ...
```

**原分类**：🔴 必须替换 — 通过复杂的 `__init__` + `strict` 参数机制，在默认（`strict=False`）情况下恢复 `$` 锚点，与直接还原 golden patch 的效果完全等价（F2P 测试场景下行为相同）。属于"功能等价冗余"。

**最终 mutation**（新设计）：
```diff
diff --git a/django/contrib/auth/validators.py b/django/contrib/auth/validators.py
index 9345c5cb0b..837d81b2f6 100644
--- a/django/contrib/auth/validators.py
+++ b/django/contrib/auth/validators.py
@@ -22,4 +22,5 @@ class UnicodeUsernameValidator(validators.RegexValidator):
         'Enter a valid username. This value may contain only letters, '
         'numbers, and @/./+/-/_ characters.'
     )
+    inverse_match = True
     flags = 0
```

**变异语义**：`inverse_match = True` 反转验证器逻辑：当正则**匹配**时抛出 ValidationError（而不是不匹配时）。因此所有合法的用户名（如 `'joe'`、`'René'`）均会被拒绝，`test_unicode_validator` 在第一个 `valid_usernames` 测试用例就会失败。`inverse_match` 属性继承自 `RegexValidator`，默认值为 `False`，添加 `True` 覆盖是一个极难察觉的语义反转，代码审查者很少关注这个属性。

## 新设计 Mutation 说明

### Group A 设计依据
两个验证器并排定义，golden patch 需同时修改两处。"部分修复"是软件开发中频发的错误模式——开发者在修复时只找到并修改了 ASCII 验证器，未察觉 Unicode 验证器有相同的 bug。这种 mutation 通过 `test_ascii_validator` 但在带有 `'trailingnewline\u000A'` 的 `test_unicode_validator` 上失败。

### Group B 设计依据
`flags` 属性控制正则编译时使用的标志位，`re.ASCII` vs `0` 决定了 `\w` 的匹配范围。开发者在修改验证器代码时，可能误将 ASCII 验证器的 `flags = re.ASCII` 视为模板复制到 Unicode 验证器中。`UnicodeUsernameValidator` 和 `ASCIIUsernameValidator` 在代码结构上几乎相同，flags 属性的差异非常微小。

### Group C 设计依据
修复 `$` → `\Z` 属于"收紧末尾锚点"，开发者理解该问题后可能会思考"是否还有其他字符应该被显式处理"，进而错误地在字符类中加入 `\s`，认为这样可以更明确地控制空白字符的行为。实际上 `\s` 包含 `\n`、`\t`、空格等，大幅放宽了字符类，导致多个原本非法的用户名变得合法。

### Group E 设计依据
`inverse_match` 是 `RegexValidator` 中一个不常用的属性（默认 `False`），大多数开发者在阅读 `UnicodeUsernameValidator` 时不会特别关注它。将 `True` 加在 `flags = 0` 之前，视觉上非常隐蔽（只有一行属性赋值）。这种属性级别的语义反转模拟了开发者在重构验证器时错误地触碰了不该改的属性的场景。
