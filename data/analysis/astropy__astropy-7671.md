# astropy__astropy-7671

## 问题背景

`minversion` 函数在比较含有 `dev`、`rc1` 等非纯数字后缀的版本字符串时会触发 Python `distutils.version.LooseVersion` 的一个已知 bug（[bpo-30272](https://bugs.python.org/issue30272)）：当比较 `LooseVersion('1.14.3') >= LooseVersion('1.14dev')` 时，由于版本列表中整数与字符串混合比较，抛出 `TypeError: '<' not supported between instances of 'int' and 'str'`。

Golden patch 的修复方案：在调用 `LooseVersion` 之前，用正则表达式从 `version`（用户传入的最低版本要求）中提取纯数字前缀，从而避免 LooseVersion 解析含 dev/rc 后缀的字符串。

## Golden Patch 语义分析

修复核心逻辑：
1. 新增 `import re`
2. 在 `minversion` 函数中，在版本比较之前，用 PEP440 兼容的正则 `'^([1-9]\d*!)?(0|[1-9]\d*)(\.(0|[1-9]\d*))*'` 对 `version` 参数做前缀匹配
3. 若匹配成功，将 `version` 替换为纯数字前缀（`m.group(0)`）
4. 这样 `'0.12dev'` → `'0.12'`，`'1.2rc1'` → `'1.2'`，从而让 LooseVersion 能正常比较

**为什么这样改是正确的**：LooseVersion 的 bug 在于比较时遇到混合类型（整数 vs 字符串）的版本分量。通过只取数字前缀，两侧的版本分量都是整数，比较不会出错。修复只作用于 `version` 参数（用户传入的最低版本要求），而不作用于 `have_version`（已安装版本），因为已安装版本通常是规范的数字版本字符串。

## 调用链分析

```
minversion(module, version, inclusive=True, version_path='__version__')
    ├── resolve_name(module_name)           # 若 module 是字符串，导入模块
    ├── getattr(module, version_path)       # 获取已安装版本 have_version
    ├── resolve_name(module.__name__, version_path)  # 有 '.' 时的版本获取路径
    ├── re.match(expr, version)             # [golden fix] 提取 version 纯数字前缀
    └── LooseVersion(have_version) >= LooseVersion(version)  # 版本比较
```

调用者（外部）：
- `astropy/__init__.py`: `minversion(numpy, __minimum_numpy_version__)`
- `astropy/io/misc/yaml.py`: `minversion(yaml, '3.12')`
- `astropy/modeling/tabular.py`: `minversion(scipy, "0.14")`
- `astropy/units/quantity_helper.py`: `minversion(scipy, "0.18")`
- `astropy/visualization/mpl_style.py`: `minversion('matplotlib', '1.5')`

数据流：`version` 参数（如 `'0.12dev'`）→ 正则提取前缀 → `'0.12'` → `LooseVersion('0.12')` → 与 `LooseVersion(have_version)` 比较。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 将整个 regex 块注释掉（含 `#    ` 缩进），明显人工痕迹 |
| B | 🔴 必须替换 | 替换 | 只注释 `if m:` 部分但保留 `expr`/`m=re.match(...)` 行，不自然且冗余 |
| C | 🔴 必须替换 | 替换 | 直接删除整个 regex 块，是 golden patch 的逆操作（直接冗余） |
| D | 🟢 保留 | 保留 | 将 regex 应用到 `have_version` 而非 `version`（双变量均错误），语义有趣且位置合理 |
| E | 🔴 必须替换 | 替换 | 用 `_use_regex=False` 隐藏参数控制修复，默认永远不执行，功能等价冗余 |

语义浅层共 0 个，替换 4 个必须替换（A、B、C、E）。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：
```diff
-    expr = '^([1-9]\\d*!)?(0|[1-9]\\d*)(\\.(0|[1-9]\\d*))*'
-    m = re.match(expr, version)
-    if m:
-        version = m.group(0)
+    #     expr = '^([1-9]\\d*!)?(0|[1-9]\\d*)(\\.(0|[1-9]\\d*))*'
+    #     m = re.match(expr, version)
+    #     if m:
+    #         version = m.group(0)
```
**分类**：🔴 必须替换
**理由**：将整个 regex 块注释掉，且注释缩进方式（`#    ` 带4个空格）明显不符合代码风格，是典型的人工调试痕迹，代码审查者一眼可识别。
**最终 mutation**：
```diff
diff --git a/astropy/utils/introspection.py b/astropy/utils/introspection.py
index e437b40c87..d226adad6d 100644
--- a/astropy/utils/introspection.py
+++ b/astropy/utils/introspection.py
@@ -146,7 +146,7 @@ def minversion(module, version, inclusive=True, version_path='__version__'):
     expr = '^([1-9]\\d*!)?(0|[1-9]\\d*)(\\.(0|[1-9]\\d*))*'
     m = re.match(expr, version)
     if m:
-        version = m.group(0)
+        have_version = m.group(0)
 
     if inclusive:
         return LooseVersion(have_version) >= LooseVersion(version)
```
**变异语义**：regex 正确匹配了 `version` 的数字前缀，但将结果赋给了 `have_version` 而非 `version`。`version` 仍保持原始值（如 `'0.12dev'`），`have_version` 被设为 `version` 的数字前缀（如 `'0.12'`）。随后 `LooseVersion('0.12') >= LooseVersion('0.12dev')` 仍会触发 TypeError。**难以发现原因**：代码结构完整，regex 逻辑看起来正确，只是赋值目标变量名写错；两个变量名相近（`version` vs `have_version`），在快速阅读时容易忽略。通过简单测试（纯数字版本），只在含 dev/rc 后缀的版本下失败。

---

### Group B — 替换
**原 mutation**：
```diff
     expr = '^([1-9]\\d*!)?(0|[1-9]\\d*)(\\.(0|[1-9]\\d*))*'
     m = re.match(expr, version)
-#     if m:
-#         version = m.group(0)
-# 
+    if inclusive:
```
**分类**：🔴 必须替换
**理由**：保留了 `expr` 和 `m = re.match(...)` 但注释掉了 `if m:` 块，造成死代码（`m` 变量被计算但从未使用），不自然且明显是人工注释。
**最终 mutation**：
```diff
diff --git a/astropy/utils/introspection.py b/astropy/utils/introspection.py
index e437b40c87..91c2c71184 100644
--- a/astropy/utils/introspection.py
+++ b/astropy/utils/introspection.py
@@ -144,7 +144,7 @@ def minversion(module, version, inclusive=True, version_path='__version__'):
     # of the version number. Match the dotted numbers only. Regex taken
     # from PEP440, https://www.python.org/dev/peps/pep-0440/, Appendix B
     expr = '^([1-9]\\d*!)?(0|[1-9]\\d*)(\\.(0|[1-9]\\d*))*'
-    m = re.match(expr, version)
+    m = re.fullmatch(expr, version)
     if m:
         version = m.group(0)
```
**变异语义**：将 `re.match`（前缀匹配）改为 `re.fullmatch`（全字符串匹配）。对于 `'0.12dev'`，`re.fullmatch` 返回 `None`（因为 `'dev'` 不符合纯数字模式），`version` 不被替换，随后 `LooseVersion` 比较触发 TypeError。对于 `'0.12'`、`'0.12.1'` 等纯数字版本，`re.fullmatch` 成功匹配，行为与原来相同。**难以发现原因**：`re.fullmatch` 是合理的 API 选择，开发者可能认为"应该匹配完整字符串"；对于纯数字版本（没有 dev/rc 后缀的），行为完全正确，只在有后缀时才出错。

---

### Group C — 替换
**原 mutation**：
```diff
-    # LooseVersion raises a TypeError when strings like dev, rc1 are part
-    # of the version number. Match the dotted numbers only. Regex taken
-    # from PEP440, https://www.python.org/dev/peps/pep-0440/, Appendix B
-    expr = '^([1-9]\\d*!)?(0|[1-9]\\d*)(\\.(0|[1-9]\\d*))*'
-    m = re.match(expr, version)
-    if m:
-        version = m.group(0)
-
```
**分类**：🔴 必须替换
**理由**：直接删除整个 regex 修复块，是 golden patch 的逆操作，属于直接冗余。
**最终 mutation**：
```diff
diff --git a/astropy/utils/introspection.py b/astropy/utils/introspection.py
index e437b40c87..7e053f881f 100644
--- a/astropy/utils/introspection.py
+++ b/astropy/utils/introspection.py
@@ -144,7 +144,7 @@ def minversion(module, version, inclusive=True, version_path='__version__'):
     # of the version number. Match the dotted numbers only. Regex taken
     # from PEP440, https://www.python.org/dev/peps/pep-0440/, Appendix B
     expr = '^([1-9]\\d*!)?(0|[1-9]\\d*)(\\.(0|[1-9]\\d*))*'
-    m = re.match(expr, version)
+    m = re.match(expr, have_version)
     if m:
         version = m.group(0)
```
**变异语义**：将 `re.match(expr, version)` 改为 `re.match(expr, have_version)`，但仍执行 `version = m.group(0)`。这意味着 `m` 是对 `have_version`（已安装版本，如 `'0.12.2'`）的匹配结果，`m.group(0)='0.12.2'`，然后 `version` 被设为 `'0.12.2'`（覆盖了原始的 `'0.12dev'`）。随后 `LooseVersion('0.12.2') >= LooseVersion('0.12.2')` 对 `'0.12dev'` 返回 True，但对 bad_versions 中的 `'1'` 也会返回 True（因为 version 被替换为 `'0.12.2'`），导致测试失败。**难以发现原因**：代码结构与 D 组相似，只改了一个变量名，逻辑看起来像是"用 have_version 的格式来规范化 version"。注意：此 mutation 与 Group D 的区别在于 D 改了两行（match 目标和赋值目标），而 C 只改了 match 目标，赋值目标仍是 `version`，导致完全不同的语义错误。

---

### Group D — 保留
**原 mutation**：
```diff
-    m = re.match(expr, version)
+    m = re.match(expr, have_version)
     if m:
-        version = m.group(0)
+        have_version = m.group(0)
```
**分类**：🟢 保留
**理由**：将 regex 应用到 `have_version` 而非 `version`，并将结果赋给 `have_version`。这实现了"对已安装版本做 stripping"而非"对要求版本做 stripping"，是一个语义上合理但方向错误的修复。修改涉及两行（match 目标和赋值目标），逻辑自洽，代码审查时难以一眼发现。
**最终 mutation**（与原相同）：
```diff
diff --git a/astropy/utils/introspection.py b/astropy/utils/introspection.py
index e437b40c8..3d9246bcc 100644
--- a/astropy/utils/introspection.py
+++ b/astropy/utils/introspection.py
@@ -144,9 +144,9 @@ def minversion(module, version, inclusive=True, version_path='__version__'):
     # of the version number. Match the dotted numbers only. Regex taken
     # from PEP440, https://www.python.org/dev/peps/pep-0440/, Appendix B
     expr = '^([1-9]\\d*!)?(0|[1-9]\\d*)(\\.(0|[1-9]\\d*))*'
-    m = re.match(expr, version)
+    m = re.match(expr, have_version)
     if m:
-        version = m.group(0)
+        have_version = m.group(0)
 
     if inclusive:
         return LooseVersion(have_version) >= LooseVersion(version)
```
**变异语义**：对 `have_version`（已安装版本）做 stripping，而 `version`（要求版本）保持原始含 dev/rc 后缀的字符串。`have_version='0.12.2'` → 全数字，regex 完整匹配 → `have_version='0.12.2'`（无变化）。`version='0.12dev'` → 未被处理 → `LooseVersion('0.12.2') >= LooseVersion('0.12dev')` → TypeError。通过所有纯数字版本测试，只在要求版本含 dev/rc 后缀时失败。

---

### Group E — 替换
**原 mutation**：
```diff
-def minversion(module, version, inclusive=True, version_path='__version__'):
+def minversion(module, version, inclusive=True, version_path='__version__', _use_regex=False):
 ...
-    expr = '^([1-9]\\d*!)?(0|[1-9]\\d*)(\\.(0|[1-9]\\d*))*'
-    m = re.match(expr, version)
-    if m:
-        version = m.group(0)
-
+    if _use_regex:
+        expr = ...
+        m = re.match(expr, version)
+        if m:
+            version = m.group(0)
```
**分类**：🔴 必须替换
**理由**：通过添加 `_use_regex=False` 隐藏参数，让修复代码永远不执行（因为默认值为 False）。这在功能上等价于删除修复，且 `_use_regex` 参数名暗示了开发者刻意设计了这个"开关"，具有明显人工设计痕迹。
**最终 mutation**：
```diff
diff --git a/astropy/utils/introspection.py b/astropy/utils/introspection.py
index e437b40c87..f6abb955ca 100644
--- a/astropy/utils/introspection.py
+++ b/astropy/utils/introspection.py
@@ -135,7 +135,7 @@ def minversion(module, version, inclusive=True, version_path='__version__'):
                          'module, or the import name of the module; '
                          'got {0!r}'.format(module))
 
-    if '.' not in version_path:
+    if '.' in version_path:
         have_version = getattr(module, version_path)
     else:
         have_version = resolve_name(module.__name__, version_path)
```
**变异语义**：将 `version_path` 的判断条件取反。原逻辑：`version_path` 不含 `.` 时用 `getattr`（简单属性），含 `.` 时用 `resolve_name`（嵌套路径）。变异后：`version_path` 含 `.` 时用 `getattr`，不含 `.` 时用 `resolve_name`。对于默认 `version_path='__version__'`（不含 `.`），会调用 `resolve_name(module.__name__, '__version__')`，尝试将 `module.__version__` 作为模块路径导入，对于测试中动态创建的 `ModuleType` 会抛出 `ImportError`（因为 `test_module` 不是真实可导入模块），导致 `minversion` 崩溃而非返回 True/False。**难以发现原因**：条件判断只差一个 `not`，逻辑反转；对于使用点路径（如 `version_path='version.info'`）的调用者，行为也会错误，但这种用法较少见，简单测试不易覆盖。

---

## 新设计 Mutation 说明

### Group A 新设计
**基于代码分析**：`minversion` 函数中 regex 块的核心操作是"将 `version` 替换为其数字前缀"。`version` 和 `have_version` 是函数中仅有的两个版本字符串变量，在赋值语句 `version = m.group(0)` 中，将 `version` 改为 `have_version` 是一个典型的"变量名混淆"错误。
**模拟的真实开发者错误**：开发者在快速修改时，可能将目标变量名写错（`version` vs `have_version` 相差4个字符），尤其在两个变量都在视野内时容易发生。这类错误在代码审查中不容易发现，因为整体逻辑结构（regex → match → 赋值）看起来完整正确。

### Group B 新设计
**基于代码分析**：`re.match` 和 `re.fullmatch` 是 Python 正则中两个常见函数，区别在于后者要求完整匹配。在这里，`re.match` 的前缀匹配行为是关键（提取数字前缀），改为 `re.fullmatch` 后，含 dev/rc 后缀的版本字符串不再匹配，修复失效。
**模拟的真实开发者错误**：开发者可能认为"应该用 `fullmatch` 来确保版本格式完整"，这是一个语义上合理但逻辑上错误的选择——因为修复的目的恰恰是处理"不完整"（含非数字后缀）的版本字符串。

### Group C 新设计
**基于代码分析**：`m = re.match(expr, version)` 后紧接 `version = m.group(0)`，两行共同实现"用 regex 提取 version 的数字前缀"。只改第一行（match 目标从 `version` 改为 `have_version`）但保留第二行，产生了"用 have_version 的数字前缀覆盖 version"的效果，这是一种微妙的跨变量污染。
**模拟的真实开发者错误**：开发者可能在理解代码时认为"需要规范化已安装版本的格式"，将 match 目标改为 `have_version`，但忘记同步修改赋值目标。与 Group D 的区别在于 D 同时修改了两行（逻辑自洽），而 C 只修改了一行（逻辑不自洽但不明显）。

### Group E 新设计
**基于代码分析**：`if '.' not in version_path:` 条件判断控制了 `have_version` 的获取路径，是 `minversion` 函数中独立于 regex 修复的另一个逻辑分支。反转这个条件会导致所有使用默认 `version_path='__version__'` 的调用出错（使用 `resolve_name` 尝试导入版本属性），而使用点路径的调用则会用 `getattr` 处理（可能也出错）。
**模拟的真实开发者错误**：`not in` 条件判断是常见的逻辑反转错误点，开发者在修改代码时可能不小心删除或添加 `not`。这个 mutation 位于 regex 修复之前的代码段，与 golden patch 修改的核心位置不同，体现了"跨逻辑分支"的变异策略。
