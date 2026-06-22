# django__django-15987

## 问题背景

`loaddata` 命令的 `fixture_dirs` 检查"某 app 的默认 `fixtures` 目录是否被误列入 `settings.FIXTURE_DIRS`"。当 `FIXTURE_DIRS` 含 `Path` 实例（而非 str）时，`app_dir`（由 `os.path.join` 得到的字符串）与列表中的 `Path` 直接 `in` 比较永不相等，重复目录检测失效。Golden patch 把 `if app_dir in fixture_dirs` 改成 `if app_dir in [str(d) for d in fixture_dirs]`——先把每个目录规整为 str 再比较。

## Golden Patch 语义分析

```python
app_dir = os.path.join(app_config.path, "fixtures")
if app_dir in [str(d) for d in fixture_dirs]:
    raise ImproperlyConfigured(
        "'%s' is a default fixture directory for the '%s' app "
        "and cannot be listed in settings.FIXTURE_DIRS." % (app_dir, app_label)
    )
```
核心语义：**比较前必须把 `fixture_dirs` 中每个元素 `str()` 规整**。`app_dir` 是 `str`，而 `FIXTURE_DIRS` 元素可能是 `Path`；`str` 与 `Path` 即使指向同一路径，`in` 的 `==` 比较也为 False（类型不同）。把右侧统一成 `[str(d) for d in fixture_dirs]` 才能正确匹配。这是单侧规整：必须规整集合侧（fixture_dirs），而非 app_dir 侧（它已是 str）。

F2P 测试 `TestFixtures.test_fixture_dirs_with_default_fixture_path_as_pathlib`：`FIXTURE_DIRS=[Path(_cur_dir)/"fixtures"]`（Path 实例），断言 loaddata 抛 `ImproperlyConfigured`（检测到默认目录被列入）。

## 调用链分析

`Command.fixture_dirs`（cached_property）遍历 `apps.get_app_configs()`，对每个 app 算 `app_dir = os.path.join(app_config.path, "fixtures")`，与 `settings.FIXTURE_DIRS` 比较：若 `app_dir` 在其中则报错。`FIXTURE_DIRS` 可由用户配置成 str 或 Path。第一处 `if len(fixture_dirs)!=len(set(...))` 检测重复，第二处 `if app_dir in ...` 检测默认目录被误列——F2P 针对的是**第二处**（Path 场景）。任何只规整第一处、或在错误的操作数上 `str()`、或不规整集合侧的改动都会让 Path 漏检。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 去掉 `str(d)` 推导式，还原 Path 漏检 bug |
| B | 🟢 高质量 | 保留 | `in`→`not in`，检测条件反转 |
| C | 🟡 语义浅层 | 替换 | 原 C 改的是第一处 dup-count（不影响 F2P，无效）；改为 `str` 用在错误操作数 |
| D | 🔴 必须替换 | 替换 | 原 D 与 A 字节完全相同；改为死规整（normalized_dirs 未使用） |
| E | 🔴 必须替换 | 替换 | 原 E 改的是第一处 dup-count（不影响 F2P，无效）；改为默认关闭开关 |

原 A=D 完全相同；原 C、E 都只改第一处 `if len != set` 的 dup-count 行——但 F2P 测的是第二处默认目录检测（只配了一个 Path、无重复），故原 C、E 不会令 F2P 失败（实测 rc=0），属无效变异。重做 C、D、E 全部精确作用于第二处的 `app_dir in ...` 比较。

## 各组 Mutation 分析

### Group A — 保留（C1 类型/数据形状：还原 bug）
```diff
-            if app_dir in [str(d) for d in fixture_dirs]:
+            if app_dir in fixture_dirs:
```
**变异语义**：去掉 `str(d)` 规整，`app_dir`（str）直接与 `fixture_dirs`（含 Path）比较。Path 元素永不等于 str，默认目录漏检，不报错。直接还原原 bug。保留。

### Group B — 保留（B3 条件反转）
```diff
-            if app_dir in [str(d) for d in fixture_dirs]:
+            if app_dir not in [str(d) for d in fixture_dirs]:
```
**变异语义**：检测条件反转。`app_dir` 确实在规整后的列表中（这是 F2P 场景），`not in` 为 False → 不报错；而正常目录（不在列表）反而触发报错。语义颠倒。保留。

### Group C — 替换（C1 类型/数据形状：str 用在错误操作数）
**原**：`if len(fixture_dirs) != len(set(fixture_dirs)) and all(isinstance(d,str)...)`（改第一处 dup-count，不影响 F2P，无效）。
**最终 mutation**：
```diff
-            if app_dir in [str(d) for d in fixture_dirs]:
+            if str(app_dir) in fixture_dirs:
```
**变异语义**：把 `str()` 错用在 `app_dir` 上（它本就是 str，`str(app_dir)` 是 no-op），而 `fixture_dirs` 侧未规整仍含 Path。`str(app_dir) in fixture_dirs` 等价于原 bug——str 与 Path 比较失败，Path 漏检。模拟"知道要 str 化、却 str 错了操作数（规整了已是 str 的一侧）"的方向性错误。精确命中 F2P。

### Group D — 替换（D1 状态：死规整）
**原**：与 A 字节完全相同（`if app_dir in fixture_dirs`）。
**最终 mutation**：
```diff
             app_dir = os.path.join(app_config.path, "fixtures")
+            normalized_dirs = [str(d) for d in fixture_dirs]
             if app_dir in fixture_dirs:
```
**变异语义**：算出了规整后的 `normalized_dirs`，但下面的 `if` 仍用未规整的 `fixture_dirs`，`normalized_dirs` 是死变量（从未使用）。代码看起来"已经做了 str 规整"，实则规整结果被丢弃、比较仍走原 bug 路径。比直接删 `str(d)`（A）更隐蔽——审查者瞥见 `normalized_dirs = [str(d) ...]` 容易误以为已修复。

### Group E — 替换（E2 隐式→显式开关）
**原**：`if len != len(set(str(d)...))` 且改报错消息（改第一处 dup-count，不影响 F2P，无效）。
**最终 mutation**：
```diff
-            if app_dir in [str(d) for d in fixture_dirs]:
+            if app_dir in ([str(d) for d in fixture_dirs] if getattr(self, "normalize_fixture_dirs", False) else fixture_dirs):
```
**变异语义**：把"是否规整"做成实例属性开关 `normalize_fixture_dirs`，默认 `False` → 走未规整的 `fixture_dirs`（原 bug），只有显式设 True 才规整。模拟"把修复做成可配置、默认却关掉"。精确命中第二处比较。

## 新设计 Mutation 说明

原 A=D 字节完全相同（都还原 `if app_dir in fixture_dirs`），且原 C、E 只改第一处 dup-count（`if len != len(set)`），而 F2P 只配单个 Path、无重复，故原 C、E 实测 rc=0（不触发失败）属无效变异。本次保留 A（还原 bug）、B（条件反转），把 C 重做为"`str()` 用在错误操作数 `app_dir`"、D 重做为"死规整 `normalized_dirs` 未使用"、E 重做为"默认关闭的 `normalize_fixture_dirs` 开关"，三者全部精确作用于第二处 `app_dir in ...` 比较。五组覆盖"删规整 / 条件反转 / 错操作数 str / 死规整 / 默认关闭开关"五个角度。全部实测：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
