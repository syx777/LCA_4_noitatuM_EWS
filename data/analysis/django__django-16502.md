# django__django-16502

## 问题背景

`runserver` 的开发服务器对 HTTP HEAD 请求返回了响应体，违反 RFC 2616 4.3（HEAD 响应不得含 body）。#26052 把 body 剥离逻辑从 Django 移除、交给生产服务器处理，但 runserver 没做，导致 HEAD 响应携带 body（并引发 "Broken pipe"）。Golden patch 在 `ServerHandler` 中：(1) `cleanup_headers` 对 HEAD 请求删除 `Content-Length` 头、并仅对非 HEAD 在无 Content-Length 时发 `Connection: close`；(2) 重写 `finish_response`，对 HEAD 请求消费迭代器但不写 body、只发送 headers，对非 HEAD 走父类默认。

## Golden Patch 语义分析

```python
def cleanup_headers(self):
    super().cleanup_headers()
    if self.environ["REQUEST_METHOD"] == "HEAD" and "Content-Length" in self.headers:
        del self.headers["Content-Length"]
    if self.environ["REQUEST_METHOD"] != "HEAD" and "Content-Length" not in self.headers:
        self.headers["Connection"] = "close"
    elif not isinstance(self.request_handler.server, socketserver.ThreadingMixIn):
        self.headers["Connection"] = "close"
    ...

def finish_response(self):
    if self.environ["REQUEST_METHOD"] == "HEAD":
        try:
            deque(self.result, maxlen=0)  # 消费迭代器，不写 body
            if not self.headers_sent:
                self.send_headers()        # 只发 headers
        finally:
            self.close()
    else:
        super().finish_response()          # 非 HEAD 正常写 body
```
核心语义：**HEAD 请求只发 headers、不写 body，且不留 Content-Length**。`finish_response` 的 HEAD 分支用 `deque(self.result, maxlen=0)` 耗尽 WSGI app 的结果迭代器（触发其副作用但丢弃内容），然后只 `send_headers()`——刻意不调 `finish_content()`（注释说明：否则 Content-Length 会默认 "0"，破坏 RFC 9110 允许的 HEAD 省略 Content-Length）。`cleanup_headers` 删 HEAD 的 Content-Length、且只对非 HEAD 发 close。判据是 `REQUEST_METHOD == "HEAD"`（字符串）。

F2P 测试 `WSGIRequestHandlerTestCase.test_no_body_returned_for_head_requests`：GET 请求断言 body 返回、有 Content-Length、无 `Connection: close`；HEAD 请求断言 body 为空（`b"\r\n"`）、无 Content-Length 头、无 `Connection: close`。

## 调用链分析

`WSGIRequestHandler` 处理请求 → `ServerHandler.run` → `finish_response`。HEAD 分支 `deque(self.result, maxlen=0)` 消费迭代器、`if not self.headers_sent: self.send_headers()`、`finally: self.close()`。`cleanup_headers`（在 headers 发送前调用）删 HEAD 的 Content-Length、设 Connection。`REQUEST_METHOD` 是字符串 `"HEAD"`/`"GET"`。判据用错（`!=`、bytes 比较）、headers_sent 守卫反转、删除 Content-Length 逻辑缺失、或把行为 gate 在开关后，都会让 HEAD 错误地返回 body 或带 Content-Length。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `finish_response` 判据 `== "HEAD"`→`!= "HEAD"`，GET/HEAD 处理对调 |
| B | 🔴 必须替换 | 替换 | 原 B 与 A 字节相同；改为反转 `headers_sent` 守卫，HEAD 不发 headers |
| C | 🟢 高质量 | 保留 | 判据 `== "HEAD"`→`== b"HEAD"`，字节串永不匹配字符串 method |
| D | 🟢 高质量 | 保留 | 删除 cleanup_headers 中删 Content-Length 的逻辑 |
| E | 🟢 高质量 | 保留 | HEAD body 剥离藏到默认关闭的 `strip_head_body` 开关后 |

原 A、B 字节完全相同（`finish_response` 的 `== "HEAD"`→`!= "HEAD"`）。保留 A、C、D、E，重做 B 为不同机制（headers_sent 守卫反转）。

## 各组 Mutation 分析

### Group A — 保留（B3 条件反转：GET/HEAD 对调）
```diff
     def finish_response(self):
-        if self.environ["REQUEST_METHOD"] == "HEAD":
+        if self.environ["REQUEST_METHOD"] != "HEAD":
```
**变异语义**：`finish_response` 判据反转。GET 请求走 HEAD 分支（消费迭代器、不写 body）→ GET 也没 body；HEAD 走 else 的 `super().finish_response()` → HEAD 反而写 body。两种方法处理对调，GET 和 HEAD 的断言都失败。保留。

### Group B — 替换（B3 条件反转：headers_sent 守卫）
**原**：与 A 字节相同（`== "HEAD"`→`!= "HEAD"`）。
**最终 mutation**：
```diff
                 deque(self.result, maxlen=0)  # Consume iterator.
                 ...
-                if not self.headers_sent:
+                if self.headers_sent:
                     self.send_headers()
```
**变异语义**：HEAD 分支里发送 headers 的守卫反转。本应"headers 尚未发送时才发"，改成"已发送时才发"——HEAD 请求通常 headers 未发，`if self.headers_sent` 为假 → 不发 headers。HEAD 响应缺失 headers，测试断言（如 Content-Length 行的检查、body 为 `b"\r\n"`）失败。判据 `== "HEAD"` 保持正确，只是分支内的 headers 发送逻辑坏了。与 A（整体判据反转）机制不同。

### Group C — 保留（C1 类型/数据形状：bytes 比较）
```diff
-            self.environ["REQUEST_METHOD"] == "HEAD"
+            self.environ["REQUEST_METHOD"] == b"HEAD"
...
-        if self.environ["REQUEST_METHOD"] == "HEAD":
+        if self.environ["REQUEST_METHOD"] == b"HEAD":
```
**变异语义**：把判据的 `"HEAD"`（str）改成 `b"HEAD"`（bytes）。`REQUEST_METHOD` 在 WSGI environ 里是 str，`"HEAD" == b"HEAD"` 永为 False → HEAD 永不被识别 → cleanup_headers 不删 Content-Length、finish_response 走 else 写 body。HEAD 响应带 body 和 Content-Length，还原 bug 表现。模拟"str/bytes 类型混淆"。保留。

### Group D — 保留（B2 删除逻辑）
```diff
     def cleanup_headers(self):
         super().cleanup_headers()
-        if (
-            self.environ["REQUEST_METHOD"] == "HEAD"
-            and "Content-Length" in self.headers
-        ):
-            del self.headers["Content-Length"]
```
**变异语义**：删除 cleanup_headers 中"HEAD 请求删 Content-Length"的逻辑。HEAD 响应保留 Content-Length 头，测试断言"无 Content-Length 头"失败。finish_response 的 body 剥离仍在，但 Content-Length 头泄漏。模拟"漏了头部清理这一半修复"。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
-    def __init__(self, stdin, stdout, stderr, environ, **kwargs):
+    def __init__(self, stdin, stdout, stderr, environ, strip_head_body=False, **kwargs):
...
+        self.strip_head_body = strip_head_body
...
     def finish_response(self):
-        if self.environ["REQUEST_METHOD"] == "HEAD":
+        if self.environ["REQUEST_METHOD"] == "HEAD" and self.strip_head_body:
```
**变异语义**：新增 `__init__` 参数 `strip_head_body`（默认 False），HEAD body 剥离只在 `strip_head_body=True` 时生效。默认实例化不传该参数 → False → HEAD 走 else 的 `super().finish_response()` 写 body。模拟"把 HEAD body 剥离做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

原 A、B 字节完全相同（`finish_response` 的 `== "HEAD"`→`!= "HEAD"`）。本次保留 A（判据整体反转，GET/HEAD 对调）、C（str/bytes 比较使 HEAD 永不匹配）、D（删除 cleanup_headers 的 Content-Length 删除逻辑）、E（默认关闭的 strip_head_body 开关），把与 A 重复的 B 重做为"反转 HEAD 分支内的 headers_sent 守卫，使 HEAD 不发送 headers"。五组分布在 `__init__`（E）、`cleanup_headers`（C/D）、`finish_response`（A/B/C/E）多处、覆盖"判据反转 / headers_sent 守卫反转 / str-bytes 比较 / 删 Content-Length 逻辑 / 默认关闭开关"五个角度。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P（`test_no_body_returned_for_head_requests`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
