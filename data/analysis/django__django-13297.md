# django__django-13297

## 问题背景

Django 3.1 在 `TemplateView` 中引入了 deprecation 机制：URL kwargs 会通过 `_wrap_url_kwargs_with_deprecation_warning()` 函数包装成惰性对象传入 `get_context_data()`，以便在访问时触发 `RemovedInDjango40Warning`。

然而，base_commit 使用的是 `@SimpleLazyObject` 装饰 `access_value` 函数，再将 `SimpleLazyObject` 实例直接赋值给 `context_kwargs[key]`。当 `get_object_or_404(Model, slug=kwargs_value)` 等 ORM 查询使用这些值作为过滤参数时，`SimpleLazyObject` 绕过了 `Field.get_prep_value()` 中的 `isinstance(value, Promise)` 检查，导致原始 `SimpleLazyObject` 被传递至 SQLite 底层驱动，引发 "Error binding parameter 0 - probably unsupported type" 错误。

Golden patch 的修复：将 `SimpleLazyObject` 替换为 `lazy()`，后者创建的代理类继承自 `Promise`，能被 `Field.get_prep_value()` 识别并正确转换，从而使 DB 查询正常工作。

## Golden Patch 语义分析

核心修复：将 `@SimpleLazyObject` + `context_kwargs[key] = access_value` 替换为：

```python
from django.utils.functional import lazy
...
context_kwargs[key] = lazy(access_value, type(value))()
```

关键区别：
- `lazy(func, *resultclasses)` 创建一个继承自 `Promise` 的代理类 `__proxy__`
- `isinstance(lazy_proxy, Promise)` → True → `Field.get_prep_value()` 会调用 `value._proxy____cast()` 获取真实值，再传给 DB
- `SimpleLazyObject` 继承自 `LazyObject`，不是 `Promise` → `Field.get_prep_value()` 不处理它 → `CharField.to_python()` 中 `isinstance(SimpleLazyObject_wrapping_str, str)` 为 True（因为 `__class__` property 代理返回 `str`）→ `to_python` 直接返回 `SimpleLazyObject` 原对象而不转换 → SQLite 驱动收到 `SimpleLazyObject` → 崩溃

`type(value)` 的重要性：让代理类正确代理被包裹值类型的方法（如 `str` 的所有方法），确保相等性检查等操作正确工作。

## 调用链分析

```
URL dispatch → View.dispatch() → TemplateView.get()
    → _wrap_url_kwargs_with_deprecation_warning(kwargs)
        → 为每个 URL kwarg 创建惰性代理 lazy(access_value, type(value))()
        → 返回 context_kwargs（全为 Promise 子类实例）
    → ContextMixin.get_context_data(**context_kwargs)
        → 用户子类可覆盖，如 ArtistView.get_context_data(artist_name=lazy_proxy)
            → Artist.objects.get(name=artist_name)
                → CharField.get_prep_value(lazy_proxy)
                    → Field.get_prep_value(lazy_proxy)  [lazy_proxy is Promise → cast]
                    → to_python(cast_result)  [plain string → OK]
                → cursor.execute(sql, ['Rene Magritte'])  [OK]
```

上游：`View.as_view()` → `View.setup()` → `View.dispatch()` → `TemplateView.get()`
下游：`ContextMixin.get_context_data()` → 用户自定义逻辑 → ORM 查询

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 语义浅层（`lazy()` 无 `()`） | 保留 | 位于关键调用点，模拟开发者漏写实例化的错误 |
| B | 🔴 必须替换（不自然 `[:-1]`） | 替换 | 截断最后一个 kwarg，人工痕迹明显 |
| C | 🔴 必须替换（base_commit 逆操作） | 替换 | 直接还原至 base_commit 的 `@SimpleLazyObject` 方案 |
| D | 🟡 语义浅层（与 A 完全重复） | 替换 | 与 A 的 diff 完全相同，冗余 |
| E | 🔴 必须替换（base_commit 逆操作，与 C 重复） | 替换 | 与 C 完全相同，均为直接还原 |

语义浅层共 2 个（A 和 D），A/D 完全相同，替换其中最弱的 D（重复项）。

## 各组 Mutation 分析

### Group A — 保留

**原 mutation**：
```diff
-        context_kwargs[key] = lazy(access_value, type(value))()
+        context_kwargs[key] = lazy(access_value, type(value))
```
**分类**：🟡 语义浅层（保留）
**理由**：`lazy(access_value, type(value))` 返回的是工厂函数（callable），而非代理实例。将函数对象存入 context_kwargs，在 DB 查询时 `isinstance(func, Promise)` → False，`isinstance(func, str)` → False，`str(func)` → `'<function ...>'`，导致查询失败。修改位置精确，模拟真实开发者漏写 `()` 的错误，且仅差一个字符难以发现。
**最终 mutation**（与原相同）：
```diff
-        context_kwargs[key] = lazy(access_value, type(value))()
+        context_kwargs[key] = lazy(access_value, type(value))
```
**变异语义**：`lazy()` 是返回工厂函数的高阶函数，`lazy(func, type)()` 才是代理实例。缺少 `()` 导致存储的是工厂本身（类型为 function），不是 Promise 实例，也不是字符串。简单的相等测试可能因为 `str(factory_func)` 等比较而通过，但 ORM 传给 DB 时类型不匹配。

---

### Group B — 替换

**原 mutation**：
```diff
-    for key, value in url_kwargs.items():
+    for key, value in list(url_kwargs.items())[:-1]:
```
**分类**：🔴 必须替换（不自然）
**理由**：`[:-1]` 截断最后一个 URL kwarg，没有任何合理的语义解释，在代码审查中会立即被发现。

**最终 mutation**：
```diff
-from django.utils.functional import lazy
+from django.utils.functional import SimpleLazyObject
...
-        context_kwargs[key] = lazy(access_value, type(value))()
+        context_kwargs[key] = SimpleLazyObject(access_value)
```
**变异语义**：将 `lazy()` proxy（Promise 子类）替换为 `SimpleLazyObject`（非 Promise）。`SimpleLazyObject` 实例因 `__class__` property 代理，`isinstance(slo, str)` 为 True，使 `CharField.to_python()` 直接返回 `SimpleLazyObject` 对象而不转换，导致 SQLite 收到无法绑定的类型。相等性测试仍能通过（`SimpleLazyObject.__eq__` 正确代理），但 ORM DB 查询失败。看起来像是开发者认为 "SimpleLazyObject 比 lazy() 更简洁" 的合理简化。

---

### Group C — 替换

**原 mutation**：
```diff
-from django.utils.functional import lazy
+from django.utils.functional import SimpleLazyObject
...
+        @SimpleLazyObject
         def access_value(key=key, value=value):
             ...
             return value
-        context_kwargs[key] = lazy(access_value, type(value))()
+        context_kwargs[key] = access_value
```
**分类**：🔴 必须替换（base_commit 逆操作）
**理由**：这完全还原了 base_commit 的有 bug 的代码。

**最终 mutation**：
```diff
-from django.utils.functional import lazy
+from django.utils.functional import SimpleLazyObject
...
-        context_kwargs[key] = lazy(access_value, type(value))()
+        context_kwargs[key] = SimpleLazyObject(lambda _f=access_value: _f())
```
**变异语义**：用 lambda 包装 `access_value`，通过默认参数正确捕获每次循环的 `access_value`，然后包装在 `SimpleLazyObject` 中。与 B 的直接 `SimpleLazyObject(access_value)` 相比，增加了 lambda 间接层，看起来像是开发者对闭包捕获的"修复"尝试。根本问题相同：`SimpleLazyObject` 不是 `Promise`，导致 DB 绑定失败。

---

### Group D — 替换

**原 mutation**：
```diff
-        context_kwargs[key] = lazy(access_value, type(value))()
+        context_kwargs[key] = lazy(access_value, type(value))
```
**分类**：🟡 语义浅层（替换，与 A 完全重复）
**理由**：与 A 的 diff 字节级相同，直接重复。

**最终 mutation**：
```diff
-from django.utils.functional import lazy
+from django.utils.functional import lazy, SimpleLazyObject
...
-        context_kwargs[key] = lazy(access_value, type(value))()
+        context_kwargs[key] = (
+            SimpleLazyObject(access_value) if isinstance(value, str)
+            else lazy(access_value, type(value))()
+        )
```
**变异语义**：对字符串类型的 URL kwarg 使用 `SimpleLazyObject`，对非字符串类型（如 `<int:pk>`）使用正确的 `lazy()`。这看起来像是一个"类型特化优化"——开发者认为字符串值不需要类型感知的 `lazy()`。实际上，所有字符串 URL kwargs（slug、name 等）都会走 `SimpleLazyObject` 路径，导致 DB 查询失败。

---

### Group E — 替换

**原 mutation**：与 C 完全相同（`@SimpleLazyObject` 装饰器 + 还原 base_commit）
**分类**：🔴 必须替换（base_commit 逆操作且与 C 重复）

**最终 mutation**：
```diff
-from django.utils.functional import lazy
+from django.utils.functional import lazy, SimpleLazyObject
...
-                RemovedInDjango40Warning, stacklevel=2,
+                RemovedInDjango40Warning, stacklevel=3,
...
-        context_kwargs[key] = lazy(access_value, type(value))()
+        context_kwargs[key] = SimpleLazyObject(access_value)
```
**变异语义**：同时修改两处：(1) `stacklevel` 从 2 改为 3，使 deprecation 警告看起来从调用方的上一层触发，影响 `test_template_params_warning` 中警告来源的 frame 信息；(2) 将 `lazy()` 替换为 `SimpleLazyObject`，导致 DB 查询失败。`stacklevel` 的改动提供了额外的迷惑性，使审查者关注警告行为，忽略了底层的类型兼容性问题。

## 新设计 Mutation 说明

**B-new 设计依据**：深入分析了 `Promise` 协议 vs `LazyObject` 的区别，以及 `Field.get_prep_value()` 中 `isinstance(value, Promise)` 检查的关键作用。`SimpleLazyObject(access_value)` 是一个"等价优化"的外表下的错误选择：对于字符串相等性测试（P2P），`SimpleLazyObject.__eq__` 正确代理；对于 DB 查询（F2P），`SimpleLazyObject` 被 `isinstance(..., str)` 误判为字符串实例，绕过类型转换，最终使 SQLite 收到无法处理的对象。

**C-new 设计依据**：通过 lambda 包装增加间接层，`lambda _f=access_value: _f()` 用默认参数捕获闭包，看起来是对闭包陷阱的"专业修复"，实际上只是改变了 `SimpleLazyObject` 的包装方式，根本类型问题不变。

**D-new 设计依据**：`isinstance(value, str)` 条件看起来是合理的类型特化，为不同 URL kwarg 类型提供不同的惰性策略。实际上所有字符串类型（包括 slug）都走 `SimpleLazyObject` 路径，覆盖了绝大多数实际使用场景。

**E-new 设计依据**：`stacklevel=3` 是一个合理的技术细节调整（当 warning 被多层函数调用时需要调整 stacklevel），与 `SimpleLazyObject` 替换组合，创造了一个多处修改的复合 mutation，使检测者需要同时关注多个变化点。
