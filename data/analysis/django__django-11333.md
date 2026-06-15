# django__django-11333

## 问题背景

Django 的 `django.urls.resolvers.get_resolver` 函数在调用时，若 `set_urlconf` 尚未被调用，会导致多个 `URLResolver` 对象被意外构造。问题根源在于：当以 `None`（即默认参数）调用 `get_resolver()` 时，与以 `settings.ROOT_URLCONF` 的实际字符串值调用 `get_resolver('myapp.urls')` 时，两者的 `lru_cache` 键不同（`None` vs `'myapp.urls'`），因此会构造出两个功能相同但身份不同的 `URLResolver`，造成不必要的性能损耗。

Golden patch 将 `get_resolver` 拆分为两个函数：
- `get_resolver`（非缓存）：负责将 `None` 规范化为 `settings.ROOT_URLCONF`，再委托给缓存层
- `_get_cached_resolver`（`@lru_cache`）：接收已规范化的 urlconf 字符串，负责缓存和构造 `URLResolver`

同时，`base.py` 的 `clear_url_caches` 改为清除 `_get_cached_resolver` 的缓存（而非原来的 `get_resolver`）。

## Golden Patch 语义分析

修复的核心逻辑：**规范化必须发生在缓存键计算之前**。

原始代码中 `get_resolver` 被 `@lru_cache` 装饰，缓存键包含 `urlconf` 参数本身。当传入 `None` 时，规范化在 `lru_cache` 内部发生，此时缓存已经以 `None` 为键记录了该次调用。若随后以 `'myapp.urls'` 传入，缓存未命中，再次构造 URLResolver。

修复后：`get_resolver(None)` → 先规范化为 `'myapp.urls'` → 再调用 `_get_cached_resolver('myapp.urls')`，与 `get_resolver('myapp.urls')` 的调用路径完全相同，缓存命中，返回同一对象。

## 调用链分析

```
base.py: resolve(path, urlconf=None)
    └─ get_urlconf() → 返回线程本地 urlconf（未设置时返回 None）
    └─ get_resolver(urlconf=None)
           └─ _get_cached_resolver(urlconf: str)  [lru_cache]
                  └─ URLResolver(RegexPattern(r'^/'), urlconf)

base.py: reverse(viewname, urlconf=None, ...)
    └─ get_resolver(urlconf)
           └─ _get_cached_resolver(urlconf)

base.py: clear_url_caches()
    └─ _get_cached_resolver.cache_clear()  [patch 后，原来是 get_resolver.cache_clear()]
    └─ get_ns_resolver.cache_clear()
    └─ get_callable.cache_clear()
```

数据流：`urlconf` 参数经 `get_resolver` 规范化后，以字符串形式作为 `_get_cached_resolver` 的缓存键，确保相同 urlconf 字符串只构造一个 `URLResolver` 实例。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 含有 `# Bug:` 注释，引入 `ResolverWrapper` 代理类，人工痕迹明显 |
| B | 缺失 | 新建 | mutations.jsonl 中不存在 Group B，需新设计 |
| C | 🔴 必须替换 | 替换 | 含有 `# BUGGY:` 注释，引入 `uuid` 和随机包装器，极度不自然 |
| D | 🟡 语义浅层 | 保留 | `maxsize=None` → `maxsize=0`，位置关键（缓存装饰器），单个语义浅层保留 |
| E | 🔴 必须替换 | 替换 | 含有 `# Bug:` 注释，使用 `r"^$"` 不合理，人工痕迹明显 |

语义浅层共 1 个（D），替换其中最弱的 floor(1/2) = 0 个，全部保留。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
diff --git a/django/urls/resolvers.py b/django/urls/resolvers.py
index 2154a46320..0e4ab74d4d 100644
--- a/django/urls/resolvers.py
+++ b/django/urls/resolvers.py
@@ -64,9 +64,22 @@ class ResolverMatch:
 
 
 def get_resolver(urlconf=None):
+    # Bug: normalize after caching instead of before
+    # This causes get_resolver() and get_resolver('urlpatterns.path_urls') to have different cache keys
+    was_none = urlconf is None
     if urlconf is None:
         urlconf = settings.ROOT_URLCONF
-    return _get_cached_resolver(urlconf)
+    result = _get_cached_resolver(urlconf)
+    # Return a wrapper that tracks whether it was called with None
+    if was_none:
+        # Create a new wrapper object to break object identity
+        class ResolverWrapper:
+            def __init__(self, resolver):
+                self._resolver = resolver
+            def __getattr__(self, name):
+                return getattr(self._resolver, name)
+        return ResolverWrapper(result)
+    return result
```

**分类**：🔴 必须替换

**理由**：含有明显人工注释 `# Bug:`，引入了功能无关的 `ResolverWrapper` 代理类和 `was_none` 变量，任何代码审查者都能立即识别这是人为注入的 bug。

**最终 mutation**：
```diff
diff --git a/django/urls/resolvers.py b/django/urls/resolvers.py
index 2154a46320..774b535ce0 100644
--- a/django/urls/resolvers.py
+++ b/django/urls/resolvers.py
@@ -63,7 +63,7 @@ class ResolverMatch:
         )
 
 
-def get_resolver(urlconf=None):
+def get_resolver(urlconf=''):
     if urlconf is None:
         urlconf = settings.ROOT_URLCONF
     return _get_cached_resolver(urlconf)
```

**变异语义**：将 `get_resolver` 的默认参数从 `None` 改为 `''`（空字符串）。这模拟了开发者认为"用空字符串作为哨兵值与 None 等价"的常见误解。当以 `get_resolver()` 无参调用时，urlconf 为 `''`，而 `if '' is None` 为 False，规范化被跳过，`_get_cached_resolver('')` 使用空字符串作为缓存键，与 `_get_cached_resolver('urlpatterns.path_urls')` 的键不同，产生两个不同的 URLResolver 实例。F2P 的 `assertIs` 断言失败。大多数不测试默认参数身份的测试仍然通过。

---

### Group B — 新建

**原 mutation**：（不存在，Group B 在 mutations.jsonl 中缺失）

**分类**：新设计

**最终 mutation**：
```diff
diff --git a/django/urls/resolvers.py b/django/urls/resolvers.py
index 2154a46320..0159e38fc1 100644
--- a/django/urls/resolvers.py
+++ b/django/urls/resolvers.py
@@ -64,9 +64,10 @@ class ResolverMatch:
 
 
 def get_resolver(urlconf=None):
+    result = _get_cached_resolver(urlconf)
     if urlconf is None:
         urlconf = settings.ROOT_URLCONF
-    return _get_cached_resolver(urlconf)
+    return result
 
 
 @functools.lru_cache(maxsize=None)
```

**变异语义**：将规范化（`None` → `ROOT_URLCONF`）移到缓存调用**之后**。这模拟了开发者重构时不小心调换了语句顺序的错误。`_get_cached_resolver(None)` 被调用，以 `None` 作为缓存键创建一个 URLResolver（URLResolver 内部会在访问时才解析 urlconf_name）。随后无论如何规范化 urlconf 变量，`result` 已经是 None 键对应的对象。`_get_cached_resolver('urlpatterns.path_urls')` 有不同的键，返回不同对象。F2P 的 `assertIs` 失败。对于普通的 URL 解析操作，`URLResolver(r'^/', None)` 在访问 `url_patterns` 时会用 `settings.ROOT_URLCONF` 解析，功能上可能正常运行，但对象身份不一致。

---

### Group C — 替换

**原 mutation**：
```diff
diff --git a/django/urls/resolvers.py b/django/urls/resolvers.py
index 2154a46320..ae8a861375 100644
--- a/django/urls/resolvers.py
+++ b/django/urls/resolvers.py
@@ -6,6 +6,7 @@ a string) and returns a ResolverMatch object which provides access to all
 attributes of the resolved URL match.
 """
 import functools
+import uuid
 import inspect
 ...
+def _wrap_urlconf(urlconf):
+    return (urlconf, uuid.uuid4())  # BUGGY: add random wrapper
```

**分类**：🔴 必须替换

**理由**：引入 `uuid` 模块，`# BUGGY:` 注释，以及随机包装器使每次调用都绕过缓存，极度不自然，任何开发者都能立即识别。

**最终 mutation**：
```diff
diff --git a/django/urls/resolvers.py b/django/urls/resolvers.py
index 2154a46320..1bb6a34562 100644
--- a/django/urls/resolvers.py
+++ b/django/urls/resolvers.py
@@ -64,13 +64,13 @@ class ResolverMatch:
 
 
 def get_resolver(urlconf=None):
-    if urlconf is None:
-        urlconf = settings.ROOT_URLCONF
     return _get_cached_resolver(urlconf)
 
 
 @functools.lru_cache(maxsize=None)
 def _get_cached_resolver(urlconf=None):
+    if urlconf is None:
+        urlconf = settings.ROOT_URLCONF
     return URLResolver(RegexPattern(r'^/'), urlconf)
```

**变异语义**：将规范化逻辑从 `get_resolver` 移入 `_get_cached_resolver` 内部。这模拟了开发者认为"规范化应该在构造函数附近进行"的架构误解。由于 `lru_cache` 在函数**调用前**就以参数值作为缓存键，将 `None` 传入会以 `None` 为键缓存，与以 `'urlpatterns.path_urls'` 为键的缓存条目不同，尽管两者最终构造出功能等价的 URLResolver。代码逻辑读起来"合理"——每个函数职责清晰，规范化在构造前发生，但缓存键层面的语义已经错误。F2P 的 `assertIs` 失败。

---

### Group D — 保留

**原 mutation**：
```diff
diff --git a/django/urls/resolvers.py b/django/urls/resolvers.py
index 2154a46320..b5fc027df2 100644
--- a/django/urls/resolvers.py
+++ b/django/urls/resolvers.py
@@ -69,7 +69,7 @@ def get_resolver(urlconf=None):
     return _get_cached_resolver(urlconf)
 
 
-@functools.lru_cache(maxsize=None)
+@functools.lru_cache(maxsize=0)
 def _get_cached_resolver(urlconf=None):
     return URLResolver(RegexPattern(r'^/'), urlconf)
```

**分类**：🟡 语义浅层（保留）

**理由**：`maxsize=0` 在 Python 中表示缓存容量为 0（不缓存任何条目），使每次调用都构造新的 URLResolver。修改位置是 `_get_cached_resolver` 的缓存装饰器——这是 golden patch 引入的核心缓存机制，修改此处直接破坏缓存的有效性。虽然是单值修改，但位置关键，能模拟开发者误用 `maxsize=0`（实际应为 `maxsize=None` 表示无限缓存）的真实错误。作为唯一的语义浅层 mutation，按规则不替换。

**最终 mutation**：与原 mutation 相同。

**变异语义**：`lru_cache(maxsize=0)` 禁用缓存，每次调用 `_get_cached_resolver` 都构造新的 `URLResolver`。`get_resolver()` 和 `get_resolver('urlpatterns.path_urls')` 返回两个不同对象，F2P 的 `assertIs` 失败。每次 URL 解析都会创建新的解析器，严重影响性能，但功能上仍然正确（URLResolver 本身仍能正确解析 URL）。

---

### Group E — 替换

**原 mutation**：
```diff
diff --git a/django/urls/resolvers.py b/django/urls/resolvers.py
index 2154a46320..eacfe3efd5 100644
--- a/django/urls/resolvers.py
+++ b/django/urls/resolvers.py
@@ -64,8 +64,12 @@ class ResolverMatch:
 
 
 def get_resolver(urlconf=None):
+    _was_none = urlconf is None
     if urlconf is None:
         urlconf = settings.ROOT_URLCONF
+    # Bug: create new resolver when urlconf was originally None
+    if _was_none:
+        return URLResolver(RegexPattern(r"^$"), urlconf)
     return _get_cached_resolver(urlconf)
```

**分类**：🔴 必须替换

**理由**：含有 `# Bug:` 注释，使用了不合理的 `r"^$"` 正则（根路径应为 `r"^/"`），引入了 `_was_none` 辅助变量，人工痕迹极为明显。

**最终 mutation**：
```diff
diff --git a/django/urls/resolvers.py b/django/urls/resolvers.py
index 2154a46320..ee8cf57a4e 100644
--- a/django/urls/resolvers.py
+++ b/django/urls/resolvers.py
@@ -64,8 +64,7 @@ class ResolverMatch:
 
 
 def get_resolver(urlconf=None):
-    if urlconf is None:
-        urlconf = settings.ROOT_URLCONF
+    urlconf = settings.ROOT_URLCONF
     return _get_cached_resolver(urlconf)
```

**变异语义**：移除条件判断，无论传入何值，`urlconf` 都被强制替换为 `settings.ROOT_URLCONF`。这模拟了开发者在重构时误以为"总是使用 ROOT_URLCONF 作为默认值"——实际上是忽略了传入参数。当测试调用 `get_resolver('urlpatterns.path_dynamic_urls')` 时，这个显式传入的 urlconf 被忽略，返回 ROOT_URLCONF 的解析器，使得 `assertIsNot(get_resolver(), get_resolver('urlpatterns.path_dynamic_urls'))` 失败（两者现在是同一对象）。代码改动极小，删除了条件分支，看起来像是"简化"，但语义完全改变。

## 新设计 Mutation 说明

### Group A 新设计
基于对 `lru_cache` 键语义的理解：golden patch 的关键是"规范化必须在缓存键确定之前完成"。将默认参数从 `None` 改为 `''` 后，无参调用时 `urlconf` 不再是 `None`，条件分支 `if urlconf is None` 不被触发，`_get_cached_resolver('')` 与 `_get_cached_resolver('urlpatterns.path_urls')` 产生两个独立缓存条目。这是 A1 策略（修改参数默认值），模拟开发者将"None 哨兵"替换为"空字符串哨兵"的常见迁移错误。

### Group B 新设计（新建）
基于对"操作顺序"的分析：`get_resolver` 的正确实现要求先规范化再缓存。将 `_get_cached_resolver(urlconf)` 提前到规范化之前，缓存调用使用了未规范化的 `None`。代码仍然会规范化 `urlconf` 变量，但此时返回值 `result` 已经绑定到 `None` 键的缓存条目。这是 B 类策略（条件逻辑/顺序错误），模拟重构时不小心调换语句顺序的错误，代码读起来"看似完整"但执行顺序错误。

### Group C 新设计
基于对"规范化在哪一层进行"的架构分析：原设计中规范化在非缓存层（`get_resolver`），确保缓存键始终为规范字符串。将规范化移入缓存函数内部后，缓存键可以是 `None` 或字符串，两者被视为不同条目，尽管构造出的 URLResolver 功能相同。这是 C1 策略（类型规范化位置错误），模拟开发者将规范化逻辑"下移"到更底层函数的设计错误，外表合理但破坏了缓存一致性。

### Group E 新设计
基于对 F2P 测试两个断言的分析：第一个断言 `assertIs(get_resolver(), get_resolver('urlpatterns.path_urls'))` 测试无参和有参调用返回同一对象；第二个断言 `assertIsNot(get_resolver(), get_resolver('urlpatterns.path_dynamic_urls'))` 测试不同 urlconf 返回不同对象。删除条件判断，始终赋值 `settings.ROOT_URLCONF`，使所有调用都返回同一缓存对象，破坏第二个断言。这是 E1 策略（改变代码行为使测试期望失效），模拟开发者"优化"时忽略传入参数的错误，单行修改，外观简洁，但使 urlconf 参数完全失效。
