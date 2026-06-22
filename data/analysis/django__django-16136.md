# django__django-16136

## 问题背景

只定义了异步 `post` 方法的 `View` 子类，被 GET 请求时崩溃：`TypeError: object HttpResponseNotAllowed can't be used in 'await' expression`。因为异步 view 的分发器会 `await` handler 返回值，而 `http_method_not_allowed` 直接返回同步的 `HttpResponseNotAllowed`，无法被 await。Golden patch 让 `http_method_not_allowed` 在 `self.view_is_async` 为真时，把响应包进一个 async 函数并返回其协程（`return func()`），同步时仍直接返回响应。

## Golden Patch 语义分析

```python
response = HttpResponseNotAllowed(self._allowed_methods())
if self.view_is_async:
    async def func():
        return response
    return func()
else:
    return response
```
核心语义：**异步 view 的 405 处理必须返回可 await 的协程对象，同步 view 返回普通响应**。`view_is_async` 区分两种模式；异步分支用内嵌 `async def func` 包装响应，`return func()` 返回协程（注意是调用 `func()` 得到协程，不是返回函数本身）。调用方对异步 view 会 `await` 该返回值，得到内部的 `response`。同步分支直接返回。三要素：(1) 正确判断 `view_is_async`；(2) 异步分支真正产出协程（`func()` 而非 `func`、且包装的是 response）；(3) 同步分支返回普通响应。

F2P 测试 `ViewTests.test_http_method_not_allowed_responds_correctly`：对 SyncView/AsyncView 分别断言返回值 `iscoroutine` 与 `is_coroutine` 期望一致，协程则 `asyncio.run` 后断言是 `HttpResponseNotAllowed`。

## 调用链分析

`View.dispatch` 对不支持的 HTTP 方法调 `http_method_not_allowed`。异步 view（`view_is_async` 为 True）的 `as_view` 包装会 `await` handler 结果——若返回普通 `HttpResponseNotAllowed` 则 `await` 非 awaitable 报 TypeError。修复让异步分支返回 `func()`（协程）。同模块 `options` 方法有完全相同的 async/sync 双分支结构。`_allowed_methods` 提供允许的方法列表。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 补充 | 新增 | 原缺有效 A；`return func()`→`return func`，返回函数对象而非协程 |
| B | 🟢 高质量 | 保留 | `if view_is_async`→`if not`，同步/异步分支对调 |
| C | 🔴 必须替换 | 替换 | 原 C 与 B 重复（仅多改了 options 处）；改为异步分支直接返回 response |
| D | 🟢 高质量 | 保留 | 删除整个 async 包装，永远返回普通响应 |
| E | 🔴 必须替换 | 替换 | 原 E 与 D 近似（删 async 包装）；改为默认关闭开关 |

原 B、C 都是把 `if view_is_async` 改成 `if not view_is_async`（C 多改一处 options），D、E 都是删除 async 包装。保留 B、D，补充 A，重做 C、E。

## 各组 Mutation 分析

### Group A — 补充（A1 接口契约：返回函数而非协程）
```diff
             async def func():
                 return response
-            return func()
+            return func
         else:
             return response
```
**变异语义**：异步分支返回 `func`（函数对象本身）而非 `func()`（调用得到的协程）。调用方 `await func` 时，`func` 是普通函数（非协程、非 awaitable），`iscoroutine(func)` 为 False，F2P 断言"异步 view 返回协程"失败。模拟"忘了调用，返回了函数引用"的经典错误——比删除整个分支隐蔽得多，async def 还在、只少了一对括号。

### Group B — 保留（B3 条件反转：分支对调）
```diff
         response = HttpResponseNotAllowed(self._allowed_methods())

-        if self.view_is_async:
+        if not self.view_is_async:
```
**变异语义**：`view_is_async` 判断反转，同步/异步分支对调。同步 view 走进 async 分支返回协程（调用方不 await → 拿到协程对象当响应，错误），异步 view 走 else 返回普通响应（被 await → TypeError）。两种模式行为正好互换。保留。

### Group C — 替换（D1 状态：异步分支不产出协程）
**原**：与 B 相同（`if not view_is_async`），仅额外在 `options` 处也改了同样一行。
**最终 mutation**：
```diff
         response = HttpResponseNotAllowed(self._allowed_methods())

         if self.view_is_async:
-
-            async def func():
-                return response
-
-            return func()
+            return response
         else:
             return response
```
**变异语义**：`view_is_async` 判断保留正确，但异步分支不再包装协程、直接 `return response`。两个分支都返回普通响应。异步 view 被 await 时仍 TypeError（response 非 awaitable）。`iscoroutine` 断言为 False，与期望 True 不符。模拟"知道要分支、却忘了在异步分支真正产出协程"——分支结构在、协程包装没了。与 D（连分支都删）不同：C 保留了分支判断、只是异步分支退化。

### Group D — 保留（B2 删除 async 包装）
```diff
-        response = HttpResponseNotAllowed(self._allowed_methods())
-
-        if self.view_is_async:
-
-            async def func():
-                return response
-
-            return func()
-        else:
-            return response
+        response = HttpResponseNotAllowed(self._allowed_methods())
+        return response
```
**变异语义**：彻底删除 async/sync 分支，无论同步异步都返回普通 `HttpResponseNotAllowed`。异步 view 被 await 时 TypeError，还原原 bug。保留。

### Group E — 替换（E2 隐式→显式开关）
**原**：与 D 近似（删除 async 包装，仅空行差异）。
**最终 mutation**：
```diff
         response = HttpResponseNotAllowed(self._allowed_methods())

-        if self.view_is_async:
+        if self.view_is_async and getattr(self, "wrap_async_405", False):

             async def func():
                 return response

             return func()
         else:
             return response
```
**变异语义**：在异步判断后追加开关 `wrap_async_405`，默认 `False`。即便 `view_is_async` 为真，因 `and False` 整体为假 → 走 else 返回普通响应（旧 bug）。只有显式设 `wrap_async_405=True` 才包装协程。模拟"把异步包装做成可配置、默认却关掉"。保留为 E。

## 新设计 Mutation 说明

原 B、C 都把 `if view_is_async` 改成 `if not view_is_async`（C 仅多改 options 一处，对 F2P 等价），D、E 都删除 async 包装（仅空行差异），五组实际只有"条件反转"和"删包装"两种机制，缺有效 A。本次保留 B（条件反转）、D（删包装），补充 A（`return func` 漏调用、返回函数而非协程）、重做 C（保留分支但异步分支直接返回 response、不产出协程）、E（默认关闭的 `wrap_async_405` 开关）。五组覆盖"漏调用 / 条件反转 / 异步分支退化 / 删包装 / 默认关闭开关"五个角度。全部实测：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
