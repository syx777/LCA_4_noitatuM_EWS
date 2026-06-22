# django__django-13837

## 问题背景

`django.utils.autoreload.get_child_arguments` 负责在 runserver 自动重载时构造子进程的启动参数。旧实现只能识别 `python -m django runserver` 这一种 `-m` 启动方式：它通过比较 `sys.argv[0]` 是否等于 `django/__main__.py` 的路径来判断。这种做法有两个缺陷：

1. 无法支持 `python -m other_pkg runserver`（即基于 Django 构建、拥有自己 `__main__` 子模块的命令行工具）。
2. 依赖模块的 `__file__` 属性，而并非所有 Python 环境都会设置 `__file__`。

golden patch 改用 Python 官方文档保证的方式判断 `-m`：顶层 `__main__` 模块的 `__spec__` 仅在以 `-m` 或目录/zip 路径启动时非 `None`，且 `__main__.__spec__.parent` 在 `-m pkg` 时等于 `pkg`、在目录/zip 启动时为空字符串。因此 `__spec__ is not None and __spec__.parent` 为真即代表 `-m pkg`，此时用 `__spec__.parent` 作为模块名重启。

## Golden Patch 语义分析

修复的核心不是"换一行判断"，而是**重新定义了"如何检测 -m 启动并恢复模块名"这一契约**：

- 判断条件从「`sys.argv[0]` 路径 == django 的 `__main__.py` 路径」改为「`__main__.__spec__ is not None and __main__.__spec__.parent`」。
- 恢复的模块名从硬编码 `'django'` 改为动态的 `__main__.__spec__.parent`，从而泛化到任意包。
- 删除了对 `django.__main__.__file__` 的依赖，提升了环境兼容性。

注意 `__spec__.parent` 必须**非空**才进入该分支：以目录/zip 启动时 parent 为 `""`，这种情况不应加 `-m`，应落到后面的分支。这是该条件中两个子条件（`is not None` 与 `and parent`）各自不可省略的原因。

## 调用链分析

- `get_child_arguments()` 被 `restart_with_reloader()`（autoreload.py:256）调用，后者把返回的 args 直接传给 `subprocess.run` 重启子进程。
- 函数内部有四条互斥分支：
  1. `-m pkg` 分支：`args += ['-m', parent]` + `sys.argv[1:]`
  2. `not py_script.exists()` 分支（Windows fallback）：再细分为 `.exe` 入口（直接返回 `[str(exe), *argv[1:]]`，忽略 `sys.executable`）和 `-script.py` 入口（返回 `[*args, str(script), *argv[1:]]`），否则 `raise RuntimeError`。
  3. else：`args += sys.argv`。
- `args` 头部恒为 `[sys.executable] + ['-W%s' % o for o in warnoptions]`，所有非 exe 分支都依赖它。
- 测试覆盖：`test_run_as_module` / `test_run_as_non_django_module`（-m 分支）、`test_warnoptions`（else 分支 + warnoptions）、`test_exe_fallback`（exe 入口）、`test_entrypoint_fallback`（script 入口）、`test_raises_runtimeerror`（RuntimeError）。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟡 语义浅层 | 保留 | 反转 `is not None` 子句，处于核心判断节点，模拟对 None 语义的误解 |
| B | 🟡 语义浅层 | 保留 | 给 parent 子句加 `not`，反转空包判断，仍在核心节点 |
| C | 🟡 语义浅层 | 替换 | `parent == ""` 与 B（`not parent`）功能等价、位置重复，属冗余 |
| D | 🟡 语义浅层 | 替换 | 删 `[sys.executable] +` 会破坏几乎所有分支测试，过于易检测 |
| E | 🟢 高质量 | 保留 | `.parent`→`.name` 属性替换，独立语义错误，难以浅层发现 |

语义浅层共 4 个（A/B/C/D），替换其中最弱的 floor(4/2)=2 个：**C、D**。

## 各组 Mutation 分析

### Group A — 保留
**原 mutation**：
```diff
-    if __main__.__spec__ is not None and __main__.__spec__.parent:
+    if __main__.__spec__ is None and __main__.__spec__.parent:
```
**分类**：🟡 语义浅层（保留）
**理由**：单符号反转（`is not None`→`is None`），但落在 golden patch 修复的核心判断节点。它模拟开发者对"`__spec__` 何时为 None"的语义记反——当真正以 `-m` 启动（`__spec__` 非 None）时反而不进分支，并在 `__spec__ is None` 时触发 `None.parent` 的属性错误。位置关键、行为微妙，符合保留标准。
**最终 mutation**：与原相同。
**变异语义**：典型 `-m django` 测试（`test_run_as_module`）下 `__spec__ is None` 为假，不加 `-m`，断言失败；逻辑反转难以从单行直观看出。

### Group B — 保留
**原 mutation**：
```diff
-    if __main__.__spec__ is not None and __main__.__spec__.parent:
+    if __main__.__spec__ is not None and not __main__.__spec__.parent:
```
**分类**：🟡 语义浅层（保留）
**理由**：给第二个子条件加 `not`，把"parent 非空才是 -m pkg"反转为"parent 为空才进分支"。这恰好模拟开发者把目录/zip 启动（parent=="")与 `-m pkg` 启动搞混。位置仍在核心判断节点，保留。
**最终 mutation**：与原相同。
**变异语义**：`-m django` 时 parent=="django" 非空，`not parent` 为假，不加 `-m`，`test_run_as_module`/`test_run_as_non_django_module` 失败。

### Group C — 替换
**原 mutation**：
```diff
-    if __main__.__spec__ is not None and __main__.__spec__.parent:
+    if __main__.__spec__ is not None and __main__.__spec__.parent == "":
```
**分类**：🟡 语义浅层（替换）
**理由**：`parent == ""` 与 Group B 的 `not parent` 在所有测试场景下行为完全等价（parent 为字符串时两者真值相同），且修改的是同一行同一子条件，属位置重复 + 功能冗余。按"与其他 mutation 最相似"的最弱标准选中替换。
**最终 mutation**（替换为 C1 — 破坏隐式类型强制转换）：
```diff
@@ -233,7 +233,7 @@ def get_child_arguments():
             # Should be executed directly, ignoring sys.executable.
             # TODO: Remove str() when dropping support for PY37.
             # args parameter accepts path-like on Windows from Python 3.8.
-            return [str(exe_entrypoint), *sys.argv[1:]]
+            return [exe_entrypoint, *sys.argv[1:]]
```
**变异语义**：去掉对 `exe_entrypoint`（一个 `Path` 对象）的 `str()` 强制转换。Windows exe-fallback 分支返回值首元素从 `str` 变成 `PosixPath/WindowsPath`。`test_exe_fallback` 断言 `== [str(exe_path), 'runserver']`，`Path != str` 使断言失败；同时 subprocess 在某些环境对 path-like 首元素行为不同。该 bug 位于一个与 `-m` 特性完全无关的旁路分支，专注于 `-m` 功能的 LLM 测试极易忽略，且代码注释里恰好写着"PY37 才需要 str()"，让删除看起来像合理的版本清理。

### Group D — 替换
**原 mutation**：
```diff
-    args = [sys.executable] + ['-W%s' % o for o in sys.warnoptions]
+    args = ['-W%s' % o for o in sys.warnoptions]
```
**分类**：🟡 语义浅层（替换）
**理由**：删掉 `[sys.executable] +` 会让 `args` 丢失解释器路径，导致 `-m` 分支、else 分支、script-fallback 分支**几乎所有测试**同时失败。这种"一改全崩"的变异在 LLM 生成的最普通测试下就会立即暴露，过于易检测，按"最容易被简单测试捕获"标准选中替换。
**最终 mutation**（替换为 D4 — 破坏资源/路径处理约定）：
```diff
@@ -234,7 +234,7 @@ def get_child_arguments():
             # TODO: Remove str() when dropping support for PY37.
             # args parameter accepts path-like on Windows from Python 3.8.
             return [str(exe_entrypoint), *sys.argv[1:]]
-        script_entrypoint = py_script.with_name('%s-script.py' % py_script.name)
+        script_entrypoint = py_script.with_name('%s_script.py' % py_script.name)
```
**变异语义**：把 Windows 控制台脚本入口的命名约定从 `<name>-script.py`（连字符，setuptools 实际生成的形式）误改为 `<name>_script.py`（下划线）。结果 `script_entrypoint.exists()` 在真实场景下永假，函数跳过 script-fallback 分支，最终落到 `raise RuntimeError('Script ... does not exist.')`。`test_entrypoint_fallback` 期望返回 `[sys.executable, str(script_path), 'runserver']`，实际抛 `RuntimeError`，测试失败。连字符/下划线之差极不显眼，且下划线在 Python 标识符里更"常见"，看起来像无害笔误，逐行审查也容易放过。

### Group E — 保留
**原 mutation**：
```diff
-        args += ['-m', __main__.__spec__.parent]
+        args += ['-m', __main__.__spec__.name]
```
**分类**：🟢 高质量（保留）
**理由**：把 `.parent`（包名，如 `django`）替换为 `.name`（完整模块名，如 `django.__main__` 或 `utils_tests.test_module.__main__`）。这是对 ModuleSpec 属性语义的真实混淆，位置在 golden patch 的第二处修改（恢复模块名），与 A/B 所在的条件判断行不同，属独立语义错误。改变了"用什么名字重启 `-m`"的契约，难以浅层检测。
**最终 mutation**：与原相同。
**变异语义**：`test_run_as_module` 期望 `-m django`，但 `__spec__.name` 给出 `django.__main__`，断言失败；`test_run_as_non_django_module` 期望 `-m utils_tests.test_module`，实际得 `utils_tests.test_module.__main__`。`.name` vs `.parent` 都是 ModuleSpec 合法属性，语法完全合理，审查时极易误以为无误。

## 新设计 Mutation 说明

两个替换 mutation（C、D）均基于对 `get_child_arguments` 四分支结构的完整分析，故意**避开 golden patch 修改的 `-m` 判断行**，转而攻击两个 Windows fallback 旁路分支：

- **C（C1 类型强制转换）**：利用 `exe_entrypoint` 是 `Path` 对象这一事实，删除 `str()` 转换。模拟开发者看到注释"PY37 才需要"后误删的真实清理错误。只有 `test_exe_fallback` 这一条专门测 exe 入口的测试会失败，专注 `-m` 主特性的测试集会全部通过。

- **D（D4 资源/路径处理）**：利用 setuptools 控制台脚本固定使用 `-script.py` 连字符命名这一外部约定，把连字符改为下划线。模拟开发者对外部资源命名约定记忆错误。错误表现（抛 RuntimeError）与根因（命名约定）分离，只有 `test_entrypoint_fallback` 覆盖该路径。

两者都满足：通过典型 `-m` 测试、只在特定旁路分支暴露、代码外观合理、与 A/B/E 修改位置互不重复（C/D 在 fallback 分支，A/B 在判断行，E 在模块名行），实现了 5 个 mutation 在函数内的位置分散。
