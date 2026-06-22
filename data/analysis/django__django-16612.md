# django__django-16612

## 问题背景

`AdminSite.catch_all_view()` 在 `APPEND_SLASH=True` 重定向时丢失查询字符串。访问 `/admin/auth/foo?id=123` 期望重定向到 `/admin/auth/foo/?id=123`，实际却跳到 `/admin/auth/foo/`（query string 没了）。根因：重定向用 `HttpResponsePermanentRedirect("%s/" % request.path)`——`request.path` 不含查询串、也不含 SCRIPT_NAME 前缀。Golden patch 改用 `request.get_full_path(force_append_slash=True)`，它返回带 query string、含 script prefix、并强制末尾加斜杠的完整路径。

## Golden Patch 语义分析

```python
if getattr(match.func, "should_append_slash", True):
    return HttpResponsePermanentRedirect(
        request.get_full_path(force_append_slash=True)
    )
```
核心语义：**重定向目标必须用 `request.get_full_path(force_append_slash=True)`，以保留查询字符串并正确处理 script 前缀，同时强制补斜杠**。`get_full_path()` 返回 `path + '?' + query_string`（含 SCRIPT_NAME），`force_append_slash=True` 让它在 path 末尾补 `/`（query 之前）。原 `"%s/" % request.path` 只拼接裸 path + 斜杠，丢了 query 与前缀。这是用对 API、且传对参数（force_append_slash）的修复。

F2P 测试 4 个：`test_missing_slash_append_slash_true_query_string`（带 `?id=1` 应保留）、`_script_name_query_string`（带 SCRIPT_NAME 前缀 + query）、`_non_staff_user_query_string`（重定向到 login 时 query 被编码进 next）、`_query_without_final_catch_all_view`。

## 调用链分析

未匹配 URL 落到 `AdminSite.catch_all_view(request, url)`。`APPEND_SLASH and not url.endswith("/")` 时尝试 `resolve("%s/" % request.path_info)`；成功且 `should_append_slash` 则返回永久重定向。重定向 URL 由 `request.get_full_path(force_append_slash=True)` 生成——它综合 SCRIPT_NAME、path、query。若用 `request.path`（裸路径）或 `request.path_info`（无前缀无 query）或 `get_full_path()`（不强制斜杠）都会丢失信息或不补斜杠。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `get_full_path(force_append_slash=True)`→`request.path + "/"`，丢 query/前缀 |
| B | 🟢 高质量 | 保留 | `not url.endswith("/")`→`url.endswith("/")`，进入条件反转 |
| C | 🔴 必须替换 | 替换 | 原 C 与 A 字节相同；改为 `"%s/" % request.path_info`（丢 query 且丢 script 前缀） |
| D | 🟢 高质量 | 保留 | 去掉 `force_append_slash=True`，URL 不补斜杠 |
| E | 🟢 高质量 | 保留 | 完整路径藏到默认关闭的 `preserve_query_string` 开关后 |

原 A、C 字节完全相同（`request.path + "/"`）。保留 A、B、D、E，重做 C 为 path_info 变体。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：用裸 path）
```diff
                     return HttpResponsePermanentRedirect(
-                        request.get_full_path(force_append_slash=True)
+                        request.path + "/"
                     )
```
**变异语义**：用 `request.path + "/"` 而非 `get_full_path(force_append_slash=True)`。`request.path` 不含查询字符串，重定向丢失 `?id=1`。这正是原 bug。带 query 的 F2P 全部失败。保留。

### Group B — 保留（B3 条件反转）
```diff
-        if settings.APPEND_SLASH and not url.endswith("/"):
+        if settings.APPEND_SLASH and url.endswith("/"):
```
**变异语义**：进入补斜杠逻辑的条件反转。原本"路径不以 / 结尾时"才尝试补斜杠重定向，改成"以 / 结尾时"。缺斜杠的 URL（正是需要重定向的）不进入该分支 → 直接 `raise Http404`，重定向根本不发生。F2P 期望 301 重定向，得到 404。保留。

### Group C — 替换（C1 值：用 path_info）
**原**：与 A 字节相同（`request.path + "/"`）。
**最终 mutation**：
```diff
-                        request.get_full_path(force_append_slash=True)
+                        "%s/" % request.path_info
```
**变异语义**：用 `request.path_info`（不含 SCRIPT_NAME 前缀、不含 query）拼 `/`。比 A（`request.path`，含前缀但无 query）更进一步——既丢 query 又丢 script 前缀。`_script_name_query_string`（期望 `/prefix/...?id=1`）尤其受影响（前缀和 query 都没了），其它 query 用例也失败。模拟"用了 path_info 而非 full path、丢了前缀和查询串"。比 A 多丢一层信息。

### Group D — 保留（A2 接口契约：漏 force_append_slash）
```diff
-                        request.get_full_path(force_append_slash=True)
+                        request.get_full_path()
```
**变异语义**：调 `get_full_path()` 但不传 `force_append_slash=True`。query string 保留了，但路径末尾不补斜杠——重定向到 `/admin/.../foo?id=1`（无斜杠），与原 URL 相同，造成重定向循环或不符合期望的 `/foo/?id=1`。F2P 断言带斜杠的目标失败。模拟"用对了方法、漏传关键参数"。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
-    def catch_all_view(self, request, url):
+    def catch_all_view(self, request, url, preserve_query_string=False):
...
-                    return HttpResponsePermanentRedirect(
-                        request.get_full_path(force_append_slash=True)
-                    )
+                    if preserve_query_string:
+                        return HttpResponsePermanentRedirect(
+                            request.get_full_path(force_append_slash=True)
+                        )
+                    else:
+                        return HttpResponsePermanentRedirect("%s/" % request.path)
```
**变异语义**：新增参数 `preserve_query_string`（默认 False），默认走 `"%s/" % request.path`（旧 bug），只有显式传 True 才用 get_full_path。URL 路由调用不传该参数 → 默认丢 query。模拟"把查询串保留做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

原 A、C 字节完全相同（`request.path + "/"`）。本次保留 A（裸 path 丢 query）、B（进入条件反转致 404）、D（漏 force_append_slash 不补斜杠）、E（preserve_query_string 默认关闭开关），把与 A 重复的 C 重做为 `"%s/" % request.path_info`——用 path_info 比 A 多丢一层 SCRIPT_NAME 前缀。五组覆盖"裸 path / 条件反转 / path_info 丢前缀 / 漏 force_append_slash / 默认关闭开关"五个角度。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
