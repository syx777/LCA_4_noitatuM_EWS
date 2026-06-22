# django__django-13810

## 问题背景

在 ASGI 上下文中，`load_middleware` 存在一个副作用 bug：当某个中间件抛出 `MiddlewareNotUsed` 时，`handler` 变量已经被更新为适配后的版本（通过 `adapt_method_mode` 调用），但该中间件被跳过。这导致 `handler` 保留了错误的适配状态，影响后续中间件的处理。

**Golden patch** 的修复策略：
1. 用临时变量 `adapted_handler` 存储适配结果（不直接赋给 `handler`）
2. 中间件构造时使用 `adapted_handler`
3. 仅在 `else` 子句（无异常）中才将 `handler = adapted_handler`

**F2P 测试** (`test_async_and_sync_middleware_chain_async_call`):
- ASGI 上下文（`is_async=True`）
- 中间件链：`SyncAndAsyncMiddleware`（外层）→ `MyMiddleware`（内层，抛出 MiddlewareNotUsed）
- 期望：响应 200 OK，日志包含 `MyMiddleware adapted.` 和 MiddlewareNotUsed 消息

## Golden Patch 语义分析

```python
# Before: 直接修改 handler（有副作用）
handler = self.adapt_method_mode(...)
mw_instance = middleware(handler)

# After: 用临时变量隔离副作用
adapted_handler = self.adapt_method_mode(...)
mw_instance = middleware(adapted_handler)
...except MiddlewareNotUsed:
    continue  # 丢弃 adapted_handler，handler 不变
else:
    handler = adapted_handler  # 仅成功时才更新 handler
```

`adapt_method_mode` 根据同步/异步上下文决定是否包装：
- `is_async=True`, `method_is_async=False` → 用 `sync_to_async` 包装，记录 "Synchronous ... adapted."
- `is_async=False`, `method_is_async=True` → 用 `async_to_sync` 包装，记录 "Asynchronous ... adapted."
- 其他情况 → 不包装，不记录

## 调用链分析

```
load_middleware(is_async=True)
  handler = async_convert_exception_to_response(get_response)
  handler_is_async = True

  for middleware in reversed(MIDDLEWARE):  # [SyncAndAsyncMiddleware, MyMiddleware]
  
    # Iteration 1: MyMiddleware (sync_capable=True, async_capable=False)
    middleware_is_async = False
    adapted_handler = adapt_method_mode(True, async_handler, True)
                    = async_to_sync(async_handler)  # Async adapted!
                    -> logs "Asynchronous middleware MyMiddleware adapted."
    middleware(adapted_handler)  -> raises MiddlewareNotUsed
    except -> continue  # handler stays as original async_handler
    handler_is_async stays True (not corrupted)

    # Iteration 2: SyncAndAsyncMiddleware (sync_capable=True, async_capable=True)
    middleware_is_async = True  # can_async in async context
    adapted_handler = adapt_method_mode(True, async_handler, True)
                    = async_handler  # already async, no wrap
    mw_instance = SyncAndAsyncMiddleware(async_handler)  # succeeds
    else: handler = adapted_handler  # handler updated

  _middleware_chain = adapt_method_mode(True, mw_chain, True)
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | 在 except 块中添加 handler = adapted_handler，原始 bug 的变体位置 |
| B | 语义浅层 | 替换 | continue → pass 导致 NameError，过于明显 |
| C | 必须替换 | 替换 | 有 `# BUG` 注释，极不自然 |
| E | 必须替换 | 替换 | 直接还原原始代码（handler = adapt_method_mode 替代 adapted_handler），直接冗余 |
| D | — | 新增 | 缺少 D 组 |

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：在 except 块中添加 `handler = adapted_handler`（即 MiddlewareNotUsed 触发时也更新 handler）→ 原始 bug 变体。

**分类**：🔴 必须替换（原始 bug 重现，质量差）

**最终 mutation**（A1 — 错误时更新 handler_is_async）：
```diff
+                handler_is_async = middleware_is_async
                 continue
             else:
                 handler = adapted_handler
```
**变异语义**：当 MiddlewareNotUsed 触发时，在 `continue` 前错误地更新 `handler_is_async = middleware_is_async`。`handler` 本身保持正确（未被更新），但 `handler_is_async` 变成了失败中间件的异步模式标志。这会导致下一个中间件的 `adapt_method_mode` 对 `handler` 的同步/异步状态产生错误判断。

F2P 场景：MyMiddleware（sync）触发 MiddlewareNotUsed → `handler_is_async = False`（错误），但实际 `handler` 仍是 async。下一迭代 SyncAndAsyncMiddleware：`adapt_method_mode(True, async_handler, False)` → 误以为 handler 是同步的 → 用 `sync_to_async` 包装 → 日志显示 "Synchronous ... SyncAndAsyncMiddleware adapted."，但测试期望 "Asynchronous ... adapted." → F2P FAIL。

模拟开发者认为"跳过中间件时应同时更新 handler 的状态追踪变量"的误解。

---

### Group B — 替换

**原 mutation**：`continue` → `pass`

**分类**：🟡 语义浅层（但有明确的失败路径）

**最终 mutation**（B2 — 移除控制流跳转）：
```diff
-                continue
+                pass
```
**变异语义**：`except MiddlewareNotUsed` 处理完后，执行流不跳转到下一次循环，而是继续向下执行 `if mw_instance is None:` 检查。但 `mw_instance` 从未在此路径中被赋值（raise 前无法完成赋值）→ `UnboundLocalError: local variable 'mw_instance' referenced before assignment`。

F2P FAIL：`test_async_and_sync_middleware_chain_async_call` 中 MyMiddleware 触发 MiddlewareNotUsed → UnboundLocalError → 不是预期的 200 响应。P2P `test_log` 等也 FAIL（同路径）。模拟开发者删除 `continue` 后未考虑到 `mw_instance` 变量作用域问题。

---

### Group C — 替换

**原 mutation**：有 `# BUG: removed continue` 等人工注释，不自然。

**分类**：🔴 必须替换

**最终 mutation**（C2 — 删除 else 子句，handler 不更新）：
```diff
-            else:
-                handler = adapted_handler
```
**变异语义**：移除 `else: handler = adapted_handler`，在中间件成功初始化后不更新 `handler`。在单一中间件或 MiddlewareNotUsed 场景下，F2P 测试通过（因为 `handler` 在循环底部会被 `convert_exception_to_response(mw_instance)` 覆写）。但在多中间件链中，当某个中间件需要适配（sync→async 或 async→sync）且后续还有其他中间件时，适配后的 `adapted_handler` 未被传递，导致后续中间件使用了未适配的 `handler`。

这是一个极其微妙的 mutation：F2P PASS（两个中间件，外层 SyncAndAsyncMiddleware 不需要特殊适配），但 P2P `test_sync_middleware_async`（单 PaymentMiddleware）也不会失败，因为循环底部的 `handler = convert_exception_to_response(mw_instance)` 覆写了 handler。实际失败需要一个多层适配链的测试。

---

### Group D — 新增

**最终 mutation**（D1 — else 子句只更新 handler_is_async，不更新 handler）：
```diff
             else:
-                handler = adapted_handler
+                handler_is_async = middleware_is_async
```
**变异语义**：成功初始化中间件后，`handler` 未更新为适配版本（`adapted_handler` 被丢弃），只更新了 `handler_is_async`。与 C 相同的核心问题：`handler` 不携带适配包装。F2P PASS（F2P 场景中 adapted_handler = handler，无需实际适配的情况下效果相同）。

实际失败场景：在多中间件链中，当中间件需要适配（如 sync-only 中间件在 async 上下文中）且该中间件后面还有其他中间件时，后续中间件拿到的是未适配的 handler，可能导致 sync/async 不匹配错误。模拟开发者认为"else 子句应该只更新状态追踪（handler_is_async），不需要更新处理器本身"。

---

### Group E — 替换

**原 mutation**：直接还原原始代码（`handler = adapt_method_mode(...)`, `mw_instance = middleware(handler)`），删除 `else: handler = adapted_handler`。直接冗余。

**分类**：🔴 必须替换

**最终 mutation**（E1 — 使用 handler 而非 adapted_handler 初始化中间件）：
```diff
-                mw_instance = middleware(adapted_handler)
+                mw_instance = middleware(handler)
```
**变异语义**：中间件实例被用未适配的 `handler` 初始化，而非 `adapted_handler`（已适配）。`else: handler = adapted_handler` 仍然存在，确保 `handler` 被更新为适配版本——但已经太晚了，中间件实例持有的 `get_response` 是错误的（未适配）引用。

F2P PASS：F2P 中，MyMiddleware 内层用未适配的 async_handler 但立即 MiddlewareNotUsed；SyncAndAsyncMiddleware 外层用 handler（已是 async）= adapted_handler（同）。  

P2P 失败场景：在 ASGI 上下文中，sync 中间件（如 PaymentMiddleware）其 `mw_instance.get_response` 是 sync handler 而非 async_to_sync 包装，当 ASGI 请求触发中间件链时，sync 中间件内部调用 `self.get_response(request)` 返回 coroutine（而非 response）→ 类型错误。模拟了最初的 bug 报告场景（TypeError: object HttpResponse can't be used in 'await' expression）。
