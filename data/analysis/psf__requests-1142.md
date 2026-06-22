# psf__requests-1142

## 问题背景

`requests.get` 总是发送 `Content-Length` 头。GET 请求本不应自动带 Content-Length（某些服务器如 amazon.com 对带 CL 的 GET 返回 503）。根因：`PreparedRequest.prepare_content_length` 开头无条件 `self.headers['Content-Length'] = '0'`，即使是无 body 的 GET/HEAD 也设。Golden patch 删掉开头那行，改成只在确有 body 时设 CL，且无 body 时仅对非 GET/HEAD 方法设 `'0'`。

## Golden Patch 语义分析

```python
def prepare_content_length(self, body):
    if hasattr(body, 'seek') and hasattr(body, 'tell'):
        body.seek(0, 2); self.headers['Content-Length'] = str(body.tell()); body.seek(0, 0)
    elif body is not None:
        self.headers['Content-Length'] = str(len(body))
    elif self.method not in ('GET', 'HEAD'):
        self.headers['Content-Length'] = '0'
```
核心语义：**删除开头无条件的 `Content-Length='0'`；无 body 时只对非 GET/HEAD 方法设 `'0'`（GET/HEAD 豁免）**。关键点：方法集 `('GET', 'HEAD')`、`not in` 否定、且这是 `elif`（仅无 body 分支）、不再有开头的无条件赋值。

F2P 测试 `test_requests.py::RequestsTestCase::test_no_content_length`：GET 和 HEAD 的 prepared request 都断言 `'Content-Length' not in headers`。

## 调用链分析

`requests.Request('GET', url).prepare()` → `prepare_content_length(body=None)` → body 为 None → `elif self.method not in ('GET','HEAD')` 为假 → 不设 Content-Length。方法集缺元素、条件反转、开头无条件赋值、else 去掉方法判断、或门控开关，都会让 GET/HEAD 被设 Content-Length。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 方法集 `('GET','HEAD')`→`('GET',)`，HEAD 不豁免 |
| B | 🟢 高质量 | 保留 | `not in`→`in`，条件反转 |
| C | 🟢 高质量 | 重做 | 开头加回无条件 `Content-Length='0'`（还原 bug） |
| D | 🟢 高质量 | 保留 | `elif ... not in (...)`→`else`，去方法判断 |
| E | 🟢 高质量 | 保留 | 豁免藏到 skip_get_head_content_length 开关后 |

原始 B==C（都 `in (...)`）。保留 A、B、D、E，重做 C 为"开头加回无条件赋值"（还原 golden 删除的那行）。

## 各组 Mutation 分析

### Group A — 保留（C1 值：方法集缺元素）
```diff
-        elif self.method not in ('GET', 'HEAD'):
+        elif self.method not in ('GET',):
```
**变异语义**：无 body 时豁免 Content-Length 的方法集从 `('GET', 'HEAD')` 缩成 `('GET',)`——HEAD 不再豁免，仍被设 `Content-Length: '0'`。F2P 断言 HEAD 无 Content-Length 失败。模拟"集合漏写一个元素"。保留。

### Group B — 保留（B3 条件反转）
```diff
-        elif self.method not in ('GET', 'HEAD'):
+        elif self.method in ('GET', 'HEAD'):
```
**变异语义**：`not in` 反转成 `in`——GET/HEAD 反而被设 Content-Length='0'、其它方法不设。条件完全颠倒。F2P（GET/HEAD 应无 CL）失败。保留。

### Group C — 重做（D1 状态：开头无条件赋值）
**原**：与 B 相同（`in (...)`）。
**最终 mutation**：
```diff
     def prepare_content_length(self, body):
+        self.headers['Content-Length'] = '0'
         if hasattr(body, 'seek') and hasattr(body, 'tell'):
```
**变异语义**：在方法开头加回 `self.headers['Content-Length'] = '0'`——正是 golden 删除的那行。所有请求（含无 body 的 GET/HEAD）开头就被设 CL='0'，后续 elif 即使豁免也已设过。还原原 bug 的根因。F2P 失败。与 B（条件反转）机制不同——这里恢复了被删的无条件赋值。重做为 C。

### Group D — 保留（B2 去方法判断）
```diff
-        elif self.method not in ('GET', 'HEAD'):
+        else:
             self.headers['Content-Length'] = '0'
```
**变异语义**：`elif self.method not in ('GET','HEAD')` 改成 `else`——去掉方法判断，无 body 的所有方法（含 GET/HEAD）都设 CL='0'。豁免逻辑被删。F2P 失败。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
-    def prepare_content_length(self, body):
+    def prepare_content_length(self, body, skip_get_head_content_length=False):
...
-        elif self.method not in ('GET', 'HEAD'):
+        elif not skip_get_head_content_length or self.method not in ('GET', 'HEAD'):
             self.headers['Content-Length'] = '0'
```
**变异语义**：GET/HEAD 豁免藏到 `skip_get_head_content_length` 参数后（默认 False）——默认 `not False or ...` 恒为真，GET/HEAD 仍被设 CL='0'。只有显式 skip=True 才豁免。调用方不传该参数。默认即 bug。F2P 失败。保留。

## 新设计 Mutation 说明

原始 B==C 字节相同（都 `in ('GET','HEAD')`）。本次保留 A（方法集缺 HEAD）、B（`not in`→`in` 反转）、D（`elif`→`else` 去方法判断）、E（skip_get_head_content_length 默认关闭开关），重做 C 为"方法开头加回无条件 `Content-Length='0'`"（恢复 golden 删除的根因行，与 B 区分）。五组覆盖"方法集缺元素 / 条件反转 / 恢复无条件赋值 / 去方法判断 / 默认关闭开关"五个角度——全部令 GET/HEAD 被错误地带上 Content-Length。全部实测（Python 3.9/requests 1.1.0，mpl34 环境）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
