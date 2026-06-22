# django__django-15851

## 问题背景

`./manage.py dbshell -- <额外参数>` 在 PostgreSQL 上，额外参数被放在数据库名之后，导致 `psql` 忽略它们（`psql: warning: extra command-line argument "-c" ignored`）。`psql` 要求所有选项必须在数据库名之前。Golden patch 调整 `settings_to_cmd_args_env` 构造 args 的顺序：先 `args.extend(parameters)`，再 `if dbname: args += [dbname]`，使额外参数排在 dbname 前面。

## Golden Patch 语义分析

```python
if port:
    args += ["-p", str(port)]
args.extend(parameters)      # ← 先加额外参数
if dbname:
    args += [dbname]         # ← dbname 放最后
```
核心语义：**额外参数 `parameters` 必须排在数据库名 `dbname` 之前**，因为 psql 把 dbname 之后的 token 当作多余实参忽略。两行的相对顺序（extend 在前、dbname 在后）是修复的全部内容。

F2P 测试 `PostgreSqlDbshellCommandTestCase.test_parameters`：`settings_to_cmd_args_env({"NAME": "dbname"}, ["--help"])` 期望 `(["psql", "--help", "dbname"], None)`——`--help` 在 `dbname` 之前。

## 调用链分析

`settings_to_cmd_args_env(cls, settings_dict, parameters)` 逐步构造 `args` 列表：可执行名、`-U/-h/-p` 等连接选项，然后是 `parameters`（用户透传的额外 psql 参数）与 `dbname`。`runshell` 调用它拿到 args 后执行 psql。修复只关心 `parameters` 与 `dbname` 两段的拼接顺序。任何让 `parameters` 重新排到 `dbname` 之后、或没真正加入 `args` 的改动都会让 F2P 断言的列表顺序错误。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 补充 | 新增 | 原缺 A 组；`if not dbname` 时才 extend，有 dbname 则丢参数 |
| B | 🟢 高质量 | 保留 | 还原原顺序（dbname 在前、extend 在后） |
| C | ➕ 补充 | 新增 | 原与 B 重复；改为 `append` 把整个列表当单元素 |
| D | ➕ 补充 | 新增 | 原与 B 重复；改为 `list(args).extend()` 死操作 |
| E | 🟢 高质量 | 保留 | 把正确顺序藏到默认关闭的 `strict_arg_order` 开关后 |

原 B/C/D 三组字节完全相同（都把顺序还原成 dbname 在前），缺 A。补充 A、把 C/D 重做为不同机制、保留 B 与 E。

## 各组 Mutation 分析

### Group A — 补充（B3 条件：有 dbname 则丢弃参数）
```diff
-        args.extend(parameters)
+        if not dbname:
+            args.extend(parameters)
         if dbname:
             args += [dbname]
```
**变异语义**：把 `extend(parameters)` 包在 `if not dbname` 里——只有没有数据库名时才加额外参数。F2P 用例提供了 `NAME="dbname"`，故 `not dbname` 为 False，`--help` 根本没进 args，结果 `["psql", "dbname"]` 缺少参数。模拟"误以为有 dbname 时参数应省略"的条件错误。

### Group B — 保留（D4 顺序还原）
```diff
-        args.extend(parameters)
         if dbname:
             args += [dbname]
+        args.extend(parameters)
```
**变异语义**：把两行顺序还原成 golden 之前——dbname 先入、parameters 后入。结果 `["psql", "dbname", "--help"]`，参数排在 dbname 后，正是原 bug。保留。

### Group C — 补充（C1 类型/数据形状：append vs extend）
**原**：与 B 重复（还原顺序）。
**最终 mutation**：
```diff
-        args.extend(parameters)
+        args.append(parameters)
```
**变异语义**：用 `append` 而非 `extend`。`append` 把整个 `parameters` 列表作为**单个元素**塞进 args，于是 args 含一个嵌套 list（`["psql", ["--help"], "dbname"]`），既顺序对不上 F2P 期望、又是错误的数据形状（list 嵌套而非展开）。模拟"append/extend 混淆"的经典列表操作错误。

### Group D — 补充（D1 状态：死操作不改 args）
**原**：与 B 重复（还原顺序）。
**最终 mutation**：
```diff
-        args.extend(parameters)
+        list(args).extend(parameters)
```
**变异语义**：`list(args)` 先复制出一个**新列表**，对副本 `extend`，原始 `args` 完全没变。参数被加到一个随即丢弃的临时列表里，`args` 里根本没有 `parameters`，结果 `["psql", "dbname"]`。代码看起来"对 args 做了 extend"，实则是死操作。比删行更隐蔽。

### Group E — 保留（E2 隐式→显式开关）
```diff
-    def settings_to_cmd_args_env(cls, settings_dict, parameters):
+    def settings_to_cmd_args_env(cls, settings_dict, parameters, strict_arg_order=False):
...
-        args.extend(parameters)
+        if strict_arg_order:
+            args.extend(parameters)
         if dbname:
             args += [dbname]
+        if not strict_arg_order:
+            args.extend(parameters)
```
**变异语义**：新增 `strict_arg_order` 参数（默认 False），默认走 else 分支——参数排到 dbname 之后（旧 bug 顺序）。只有显式传 `strict_arg_order=True` 才得到正确顺序。F2P 调用不传该参数 → 默认 False → 失败。模拟"把修复做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

原 B/C/D 三组字节完全相同（都把顺序还原成 dbname 在前），缺 A。本次保留 B（还原顺序）、E（默认关闭开关），补充 A（`if not dbname` 才 extend）、把重复的 C 改为 `append`（错误数据形状）、D 改为 `list(args).extend()`（死操作）。五组覆盖"条件丢参数 / 顺序还原 / append-vs-extend / 死操作 / 默认关闭开关"五个角度。全部实测：golden 通过、五个变异均令 F2P（`test_parameters`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
