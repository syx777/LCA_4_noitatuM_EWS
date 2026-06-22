# django__django-13344

## 问题背景

在 Django ASGI 模式下，使用 `MiddlewareMixin` 的子类（如 `SecurityMiddleware`、`CacheMiddleware`、`SessionMiddleware`）若在自己的 `__init__` 中手动调用 `_get_response_none_deprecation` 和 `self.get_response = get_response`，但**遗漏了 `_async_check()`**，导致 `self._is_coroutine` 未被设置。当 `get_response` 是协程函数时，`asyncio.iscoroutinefunction(middleware_instance)` 返回 False，中间件以同步模式处理请求——`get_response(request)` 返回的是协程对象而非 `HttpResponse`，协程对象被传给下游中间件的 `process_response`，从而出现 bug。

## Golden Patch 语义分析

修复核心：将各子类 `__init__` 中的手动初始化（`_get_response_none_deprecation` + `self.get_response = get_response` + 可能遗漏的 `_async_check()`）统一替换为 `super().__init__(get_response)`。

`MiddlewareMixin.__init__` 完整流程：
1. `_get_response_none_deprecation(get_response)` — 废弃警告
2. `self.get_response = get_response` — 保存引用
3. `self._async_check()` — **关键**：若 get_response 是协程函数，设置 `self._is_coroutine = asyncio.coroutines._is_coroutine`

`_async_check()` 设置的 `_is_coroutine` 被 `asyncio.iscoroutinefunction()` 检查，使中间件实例本身被识别为协程函数，从而在 `__call__` 中切换到 `__acall__`（真正 await get_response）。

`CacheMiddleware` 的修复还涉及：改用 try/except 模式只在 kwargs 显式提供时覆盖继承的默认值（`key_prefix`, `cache_alias`, `cache_timeout`），以正确利用 `UpdateCacheMiddleware`/`FetchFromCacheMiddleware` 的初始值。

## 调用链分析

```
SecurityMiddleware.__init__(get_response=async_fn)
  └── super().__init__(get_response)  ← 修复前各子类手动写这3行：
        ├── _get_response_none_deprecation(get_response)
        ├── self.get_response = get_response
        └── self._async_check()
              └── if iscoroutinefunction(self.get_response):
                      self._is_coroutine = asyncio.coroutines._is_coroutine

SecurityMiddleware.__call__(request)
  └── if iscoroutinefunction(self.get_response):  ← 检查 get_response 本身（不检 _is_coroutine）
        return self.__acall__(request)             ← async 处理
  └── [否则] 同步处理，get_response(request) = coroutine 对象 → bug

asyncio.iscoroutinefunction(middleware_instance)
  └── checks: hasattr(obj, '_is_coroutine') and obj._is_coroutine is asyncio.coroutines._is_coroutine
  └── 由 _async_check() 设置后返回 True
```

注：`__call__` 自己再次检查 `iscoroutinefunction(self.get_response)` 而非 `_is_coroutine`，但 `_is_coroutine` 属性决定了 `asyncio.iscoroutinefunction(middleware_instance)` 的返回值（对外可见的 ASGI 协议检测）。F2P 测试检查的是 `asyncio.iscoroutinefunction(middleware_instance)` 本身。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | `_async_check` 中添加 do-nothing 覆盖，明显人工痕迹 |
| B | 🔴 必须替换 | 替换 | 两文件修改：cache.py `is None` 条件反转 + security.py 删 super()，多处直接还原 |
| C | 🔴 必须替换 | 替换 | 删除 UpdateCacheMiddleware 的 super()，直接还原 |
| D | 🔴 必须替换 | 替换 | 注释掉 cache，删除 security 的 super()，双重还原 + 注释不自然 |
| E | 🔴 必须替换 | 替换 | 添加 `_call_super` 旗标，虚构参数极为不自然 |

全部5个必须替换，各自设计高质量替代。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**（两文件）：
- `cache.py`: `if cache_timeout is not None:` → `if cache_timeout is None:`
- `security.py`: 添加 `def _async_check(self): pass` 覆盖

**分类**：🔴 必须替换 — 覆盖 `_async_check` 方法明显是人工注入 bug

**最终 mutation**（`django/utils/deprecation.py`）：

```diff
diff --git a/django/utils/deprecation.py b/django/utils/deprecation.py
index b2c681b33c..3f768876cf 100644
--- a/django/utils/deprecation.py
+++ b/django/utils/deprecation.py
@@ -102,7 +102,7 @@ class MiddlewareMixin:
         If get_response is a coroutine function, turns us into async mode so
         a thread is not consumed during a whole request.
         """
-        if asyncio.iscoroutinefunction(self.get_response):
+        if not asyncio.iscoroutinefunction(self.get_response):
             # Mark the class as async-capable, but do the actual switch
             # inside __call__ to avoid swapping out dunder methods
             self._is_coroutine = asyncio.coroutines._is_coroutine
```

**变异语义**：将 `_async_check()` 中的条件取反——只有当 `get_response` **不是**协程函数时才设置 `_is_coroutine`。效果：同步中间件被错误标记为协程，异步中间件（需要修复的情形）不被标记，`asyncio.iscoroutinefunction(middleware_instance)` 对异步中间件返回 False。F2P 测试的 `assertIs(asyncio.iscoroutinefunction(middleware_instance), True)` 失败。此 mutation 跨所有使用 MiddlewareMixin 的中间件生效（影响范围最广）。难以发现：`_async_check` 里的 `not` 需要深入理解 `_is_coroutine` 标记语义才能识别问题。

---

### Group B — 替换

**原 mutation**（两文件）：
- `cache.py`: `if cache_timeout is not None:` → `if cache_timeout is None:`
- `security.py`: 删除 `super().__init__(get_response)`（直接还原）

**分类**：🔴 必须替换

**最终 mutation**（`django/contrib/sessions/middleware.py`）：

```diff
diff --git a/django/contrib/sessions/middleware.py b/django/contrib/sessions/middleware.py
index cb8c1ff45b..a36709f30a 100644
--- a/django/contrib/sessions/middleware.py
+++ b/django/contrib/sessions/middleware.py
@@ -13,7 +13,7 @@ class SessionMiddleware(MiddlewareMixin):
     # RemovedInDjango40Warning: when the deprecation ends, replace with:
     #   def __init__(self, get_response):
     def __init__(self, get_response=None):
-        super().__init__(get_response)
+        super().__init__(None)
         engine = import_module(settings.SESSION_ENGINE)
         self.SessionStore = engine.SessionStore
```

**变异语义**：`SessionMiddleware` 向父类传 `None` 而非实际的 `get_response`。`MiddlewareMixin.__init__` 接收 `None`：（1）触发废弃警告；（2）`self.get_response = None`；（3）`_async_check()` 检查 `iscoroutinefunction(None)` → False，不设置 `_is_coroutine`。最终 `asyncio.iscoroutinefunction(session_middleware_instance)` 返回 False，且 `self.get_response` 为 None（调用时崩溃）。F2P 测试的 `assertIs(asyncio.iscoroutinefunction(middleware_instance), True)` 对 `SessionMiddleware` 失败。难以发现：`super().__init__(None)` 与 `super().__init__(get_response)` 仅参数不同，快速审查易误以为是合理的"初始化顺序调整"。

---

### Group C — 替换

**原 mutation**：删除 `UpdateCacheMiddleware` 的 `super().__init__(get_response)`（直接还原）

**分类**：🔴 必须替换

**最终 mutation**（`django/middleware/security.py`）：

```diff
diff --git a/django/middleware/security.py b/django/middleware/security.py
index 44921cd22b..4ae1fa8025 100644
--- a/django/middleware/security.py
+++ b/django/middleware/security.py
@@ -10,6 +10,7 @@ class SecurityMiddleware(MiddlewareMixin):
     #   def __init__(self, get_response):
     def __init__(self, get_response=None):
         super().__init__(get_response)
+        self._is_coroutine = None
         self.sts_seconds = settings.SECURE_HSTS_SECONDS
```

**变异语义**：`SecurityMiddleware.__init__` 在调用 `super().__init__(get_response)` 之后立即将 `self._is_coroutine` 设为 `None`。`super()` 已正确设置 `_is_coroutine = asyncio.coroutines._is_coroutine`（若 get_response 是协程），但随后被 `None` 覆盖。`asyncio.iscoroutinefunction()` 检测时 `obj._is_coroutine is asyncio.coroutines._is_coroutine` → False，中间件不再被识别为协程。难以发现：`super().__init__` 调用存在且正确，`_is_coroutine = None` 看似无害的"初始化"赋值，需要了解 `_is_coroutine` 是哨兵值比较（`is`）才能识别问题。

---

### Group D — 替换

**原 mutation**（两文件）：
- `cache.py`: 注释掉 `self.cache = caches[self.cache_alias]`
- `security.py`: 删除 `super().__init__(get_response)`

**分类**：🔴 必须替换

**最终 mutation**（`django/middleware/cache.py`）：

```diff
diff --git a/django/middleware/cache.py b/django/middleware/cache.py
index 97bb199eff..7f14a5d37c 100644
--- a/django/middleware/cache.py
+++ b/django/middleware/cache.py
@@ -65,6 +65,8 @@ class UpdateCacheMiddleware(MiddlewareMixin):
     #   def __init__(self, get_response):
     def __init__(self, get_response=None):
         super().__init__(get_response)
+        if hasattr(self, '_is_coroutine'):
+            del self._is_coroutine
         self.cache_timeout = settings.CACHE_MIDDLEWARE_SECONDS
```

**变异语义**：`UpdateCacheMiddleware.__init__` 调用 `super().__init__(get_response)` 后，检查并删除 `_is_coroutine` 属性。如果 `get_response` 是协程，`super()` 会设置 `_is_coroutine`，但紧接着被删除。删除后 `asyncio.iscoroutinefunction(instance)` 返回 False。`CacheMiddleware` 继承自 `UpdateCacheMiddleware`，其 MRO 会调用 `UpdateCacheMiddleware.__init__`，因此 `CacheMiddleware`、`UpdateCacheMiddleware` 实例均受影响。难以发现：看似是防御性的"清理代码"（`if hasattr → del`），且 `super()` 本身正确调用，问题隐藏在之后的状态清理中。

---

### Group E — 替换

**原 mutation**：`SecurityMiddleware.__init__` 添加 `_call_super=False` 参数控制 super() 调用

**分类**：🔴 必须替换

**最终 mutation**（`django/middleware/cache.py`）：

```diff
diff --git a/django/middleware/cache.py b/django/middleware/cache.py
index 97bb199eff..0c8a0d32c7 100644
--- a/django/middleware/cache.py
+++ b/django/middleware/cache.py
@@ -193,6 +193,6 @@ class CacheMiddleware(UpdateCacheMiddleware, FetchFromCacheMiddleware):
         except KeyError:
             pass
 
-        if cache_timeout is not None:
+        if cache_timeout is None:
             self.cache_timeout = cache_timeout
         self.page_timeout = page_timeout
```

**变异语义**：`CacheMiddleware.__init__` 中将 `if cache_timeout is not None:` 改为 `if cache_timeout is None:`。原逻辑：只有显式传入 `cache_timeout` 时才覆盖继承的默认值（`UpdateCacheMiddleware` 已设置 `self.cache_timeout = settings.CACHE_MIDDLEWARE_SECONDS`）。变异后：当 `cache_timeout=None`（默认不传）时，执行 `self.cache_timeout = None`，覆盖继承的合理默认值为 None；当实际传入非 None 的 `cache_timeout` 时，不覆盖（保持继承值不变）。`test_constructor` 中 `as_view_decorator = CacheMiddleware(my_view)` 期望 `cache_timeout` 等于系统默认值（30 秒），但实际得到 None。此 mutation 只影响 cache 配置，不影响 ASGI 协程检测，针对不同的 F2P 测试路径。难以发现：`is not None` 与 `is None` 逻辑完全对称，需要结合调用链中的继承关系才能理解哪个方向才正确。

## 新设计 Mutation 说明

### Group A（B3，deprecation.py）
- **分析基础**：`_async_check()` 是设置 `_is_coroutine` 的唯一入口，条件取反使所有中间件的 async 检测全部反向。修改位置在 MiddlewareMixin 基类，影响范围覆盖所有子类。
- **错误模拟**：开发者误以为"非协程函数时才需要标记为协程"（逻辑倒置理解）。

### Group B（C1，sessions/middleware.py）
- **分析基础**：`super().__init__(None)` 和 `super().__init__(get_response)` 形式完全相同，仅参数值不同，typo 式错误。None 使 `get_response` 未被正确传播，导致 async 检测失效且 get_response 无效。
- **错误模拟**：开发者在重构 super() 调用时误用默认值 `None` 而非传入参数名。

### Group C（D1，security.py）
- **分析基础**：`_is_coroutine` 是 asyncio 框架用于识别协程函数的哨兵值。super() 正确设置后被后续赋值覆盖 — 典型的"初始化后状态被覆盖"错误。
- **错误模拟**：开发者添加了看似无害的防御性初始化 `self._is_coroutine = None`，不了解该属性的哨兵语义。

### Group D（D1/D2，cache.py UpdateCacheMiddleware）
- **分析基础**：删除 `_is_coroutine` 与将其设为 None 效果相同（都使 iscoroutinefunction 返回 False），但形式上是"清理多余状态"。通过 MRO，CacheMiddleware 也受影响。
- **错误模拟**：开发者误认为 `_is_coroutine` 是 __init__ 时设置的"脏状态"，需要在子类初始化中清除。

### Group E（B3，cache.py CacheMiddleware）
- **分析基础**：`cache_timeout is not None` 是"只在显式提供时覆盖"的标准模式，与 try/except KeyError 处理 key_prefix/cache_alias 的逻辑一致。反转为 `is None` 破坏了这个语义。
- **错误模拟**：开发者误理解为"当 cache_timeout 为 None（未提供）时才应该设置"，与 try/except 模式的语义混淆。
