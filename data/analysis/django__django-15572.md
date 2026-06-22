# django__django-15572

## 问题背景

Django 3.2.4+ 的 autoreload 在 `TEMPLATES` 的 `DIRS` 含空字符串时失效。新代码用 `pathlib.Path` 规范化模板目录，空字符串 `""` 经 `cwd / to_path("")` 变成 cwd 本身，使 `template_changed` 把任何文件都当模板变更（或反之失效）。Golden patch 在收集模板目录时过滤掉假值目录：第一处 `if dir`，第二处 `if directory and not is_django_path(directory)`。

## Golden Patch 语义分析

```python
items.update(cwd / to_path(dir) for dir in backend.engine.dirs if dir)
...
items.update(
    cwd / to_path(directory)
    for directory in loader.get_dirs()
    if directory and not is_django_path(directory)
)
```
核心语义：**收集模板目录前过滤掉空/假值目录**，避免 `cwd / to_path("")` 退化成 cwd。两处协同：`backend.engine.dirs` 的 `if dir` 与 loader 的 `if directory and ...`。

F2P 测试 `TemplateReloadTests.test_template_dirs_ignore_empty_path`：`DIRS: [""]`，断言 `get_template_directories() == set()`（空目录被过滤，结果为空）。

## 调用链分析

`get_template_directories` 遍历 DjangoTemplates backend：从 `backend.engine.dirs` 与各 loader 的 `get_dirs()` 收集目录，用 `cwd / to_path(x)` 规范化加入 `items`。测试只配 `DIRS=[""]`（即 `backend.engine.dirs`），故第一处 `if dir` 过滤是关键——它必须把空字符串排除，否则 `cwd / to_path("")` == cwd 进入结果集，`== set()` 失败。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 补充 | 新增 | 原缺 A 组（`if dir is not None`，空串仍通过） |
| B | 🟢 高质量 | 保留 | 删除 `if dir` 过滤 |
| C | ➕ 补充 | 新增 | 原缺 C 组（`if len(dir) >= 0` 恒真） |
| D | 🔴 必须替换 | 替换 | 原 D=B（删 if dir）；改为 `if not dir`（逻辑反转） |
| E | 🔴 必须替换 | 替换 | 原 E=B（删 if dir）；改为开关 gate |

原 B=D=E 三者完全相同（删 `if dir`）。保留 B，补充 A、C，重做 D、E。

## 各组 Mutation 分析

### Group A — 补充（B-边界：is not None）
```diff
-        items.update(cwd / to_path(dir) for dir in backend.engine.dirs if dir)
+        items.update(cwd / to_path(dir) for dir in backend.engine.dirs if dir is not None)
```
**变异语义**：过滤条件从真值判断 `if dir` 改成 `if dir is not None`。空字符串 `""` 不是 None，故仍通过过滤 → `cwd / to_path("")` == cwd 进入 items，结果非空。模拟"用 is not None 判空、漏掉空字符串"的经典边界错误。

### Group B — 保留（B2 移除过滤）
```diff
-        items.update(cwd / to_path(dir) for dir in backend.engine.dirs if dir)
+        items.update(cwd / to_path(dir) for dir in backend.engine.dirs)
```
**变异语义**：删除 `if dir` 过滤，空字符串目录直接进入。直接还原原 bug。保留。

### Group C — 补充（C1 类型/数据形状：len>=0 恒真）
```diff
-        items.update(cwd / to_path(dir) for dir in backend.engine.dirs if dir)
+        items.update(cwd / to_path(dir) for dir in backend.engine.dirs if len(dir) >= 0)
```
**变异语义**：用 `len(dir) >= 0` 替代真值判断。任何字符串长度都 `>= 0`（空串长度 0 也满足），过滤恒为真，空目录通过。模拟"想用 len 判非空却写成 `>= 0`"的 off-by-one。

### Group D — 替换（B3 逻辑反转）
**原**：与 B 重复（删 if dir）。
**最终 mutation**：
```diff
-        items.update(cwd / to_path(dir) for dir in backend.engine.dirs if dir)
+        items.update(cwd / to_path(dir) for dir in backend.engine.dirs if not dir)
```
**变异语义**：过滤条件反转为 `if not dir`——只保留**空/假值**目录，过滤掉真实目录。空字符串 `""` 反而通过（`not "" == True`），结果含 cwd，且真实目录全被丢弃。模拟 `if dir`/`if not dir` 反转。

### Group E — 替换（E2 隐式→显式开关）
**原**：与 B 重复（删 if dir）。
**最终 mutation**：
```diff
-        items.update(cwd / to_path(dir) for dir in backend.engine.dirs if dir)
+        items.update(cwd / to_path(dir) for dir in backend.engine.dirs if dir or not getattr(backend, 'skip_empty_dirs', False))
```
**变异语义**：把过滤改成 `if dir or not skip_empty_dirs`，开关 `skip_empty_dirs` 默认 False → `not False == True` → 条件恒真，空目录不被过滤。只有显式设 `skip_empty_dirs=True` 才恢复过滤。模拟"把过滤做成可配置、默认却关掉"。

## 新设计 Mutation 说明

原 B=D=E 三者完全相同（删 `if dir`）。本次保留 B（删过滤），补充 A（`is not None` 漏空串）、C（`len>=0` 恒真），重做 D（`if not dir` 逻辑反转）、E（默认关闭的 skip_empty_dirs 开关）。五组覆盖"is not None / 删除 / len>=0 / 逻辑反转 / 默认关闭开关"五个角度。全部实测：golden 通过、变异令 F2P（`test_template_dirs_ignore_empty_path`）失败、`base→golden→test_patch` 后干净应用。
