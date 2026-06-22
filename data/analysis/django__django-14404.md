# django__django-14404 Mutation Curation 分析

## 问题背景

`AdminSite.catch_all_view()` 在 `APPEND_SLASH` 生效、URL 缺少尾部斜杠时会返回 301 重定向。
原始 bug：重定向目标用 `'%s/' % request.path_info` 构造，而 `path_info` 不包含 SCRIPT_NAME
（部署前缀 / FORCE_SCRIPT_NAME）。因此在带脚本前缀的部署下，重定向会丢失前缀，跳转到错误地址。

## Golden Patch 语义分析

```python
# before
path = '%s/' % request.path_info
match = resolve(path, urlconf)
...
return HttpResponsePermanentRedirect(path)
# after
match = resolve('%s/' % request.path_info, urlconf)   # resolve 仍用 path_info（正确，urlconf 不含 script_name）
...
return HttpResponsePermanentRedirect('%s/' % request.path)  # 重定向用 request.path（含 script_name）
```

关键点：URL 解析（resolve）必须用 `path_info`（不含前缀），而对外的重定向 Location 必须用
`request.path`（含前缀）。Golden 把两者拆开，是修复的语义核心。

## 调用链分析

请求缺斜杠 URL → admin 的 catch-all pattern → `catch_all_view(request, url)` →
`resolve('%s/'%path_info)` 命中目标视图 → `should_append_slash` 为真 →
`HttpResponsePermanentRedirect('%s/'%request.path)`。测试通过 `SCRIPT_NAME='/prefix/'`
或 `FORCE_SCRIPT_NAME='/prefix/'` 注入前缀，断言 Location 为 `/prefix + known_url`。

## F2P 测试

`tests/admin_views/tests.py::AdminSiteFinalCatchAllPatternTests` 新增两个测试：
- `test_missing_slash_append_slash_true_script_name`（`SCRIPT_NAME='/prefix/'`）
- `test_missing_slash_append_slash_true_force_script_name`（`FORCE_SCRIPT_NAME='/prefix/'`）
均断言重定向到 `'/prefix' + known_url`，`status_code=301`。

## 替换决策总览

| 组 | 原 diff 语义 | 分类 | 决策 | 最终变异机制 |
|----|--------------|------|------|--------------|
| A | `request.path` → `request.path_info`（golden 精确反向） | 🔴 MUST REPLACE | 替换 | `get_full_path_info()` 辅助方法回退 |
| B | `should_append_slash` 条件取反（单 token） | 🟡 SEMANTIC-SHALLOW | 保留 | 关键控制流边界，M=1 → floor(1/2)=0 替换 |
| C | 与 A 字节完全相同（重复） | 🔴 MUST REPLACE（重复） | 替换 | `SCRIPT_NAME + path_info` 拼接（双斜杠） |

M(shallow)=1 → 替换 floor(1/2)=0 个 shallow。🔴 共 2 个（A 直接反向、C 重复）全部替换。

## 各组 Mutation 分析

### Group A
- 原 diff：`return HttpResponsePermanentRedirect('%s/' % request.path)` → `... request.path_info)`。
- 分类：🔴。这是 golden 修复的精确逆操作（buggy 原状），属直接冗余，必须替换。
- 最终 diff：改用 `request.get_full_path_info(force_append_slash=True)`。
- 变异语义：看似向 request API 收敛的自然重构，但 `get_full_path_info` 基于 `path_info`
  构造（剥离 script name），等价于重新引入原 bug。无前缀的测试结果完全一致，仅带 script
  前缀的 F2P 用例能捕获。

### Group B（保留）
- 原 diff：`if getattr(...should_append_slash..., True):` → `if not getattr(...):`。
- 分类：🟡 单 token 取反，但位于真实控制流边界（是否对该视图追加斜杠）。
- 决策：保留。M=1，应替换 floor(1/2)=0 个；该节点建模真实边界错误，予以保留。
- 变异语义：使重定向分支仅对显式 opt-out（`should_append_slash=False`）的视图触发，
  反转常见路径。对罕见 opt-out 视图行为正确，需要断言普通 admin URL 确实发生重定向的
  测试才能发现。全模块失败 6 个（含 F2P 及兄弟 append-slash 用例）。

### Group C
- 原 diff：与 A 字节完全相同（`request.path` → `request.path_info`），重复项。
- 分类：🔴（重复）。必须替换为与 A 正交的失败模式。
- 最终 diff：`script_name = request.META.get('SCRIPT_NAME', '')`；
  `return HttpResponsePermanentRedirect('%s%s/' % (script_name, request.path_info))`。
- 变异语义：伪装成"显式感知 script name"的修复，但 `request.path` 本就已含 SCRIPT_NAME，
  此处手动再拼接，在真实前缀下产生畸形的 `/prefix//test_admin/...`（双斜杠）。SCRIPT_NAME
  为空时与正确实现一致，仅带前缀的 F2P 用例能检测到畸形 URL。

## 新设计 Mutation 说明

A 与 C 失败模式正交：
- A：完全丢失前缀（`/test_admin/...`，缺 `/prefix`）。
- C：保留前缀但多一个斜杠（`/prefix//test_admin/...`）。
两者均在无 script 前缀时通过常规测试，仅在 SCRIPT_NAME / FORCE_SCRIPT_NAME 场景失败，
失败的具体 URL 形态不同，提升了多样性。

## 验证结果（真实运行）

- 环境：复制 base repo → `patch -p1` 应用 golden + test_patch（均 rc0）→ commit。
- Baseline（golden，无变异）：`admin_views.tests` 全模块 **344 passed (15 skipped), OK**。
  两个新 F2P 测试单独运行 **PASS**。
- F2P 模块：`admin_views.tests`（dotted path of test file，新测试位于
  `AdminSiteFinalCatchAllPatternTests`）。
- 各最终变异全模块结果：
  - A：FAILED，failures=2 —— 仅两个 F2P script-name 测试失败，P2P 全过。
  - B：FAILED，failures=6 —— F2P + 兄弟 append-slash 用例（保留的 shallow，预期）。
  - C：FAILED，failures=2 —— 仅两个 F2P script-name 测试失败，P2P 全过。
- 三个 diff 均通过 `git apply --check` 与 `patch -p1 --dry-run`，且以换行结尾。
- 三个修改文件 `py_compile` 均通过。
