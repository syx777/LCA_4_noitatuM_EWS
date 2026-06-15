# astropy__astropy-14365

## 问题背景

`ascii.qdp` 格式读取器假设 QDP 文件中的命令（如 `READ SERR 1 2`）必须是大写形式。但实际的 QDP 程序本身对大小写不敏感，许多用户手写的 QDP 文件会使用小写命令（如 `read serr 1 2`）。当遇到小写命令时，解析器抛出 `ValueError: Unrecognized QDP line: read serr 1 2`，无法正确读取文件。

## Golden Patch 语义分析

Golden patch 修复了两处独立的大小写敏感问题：

1. **`_line_type()` 函数（第 71 行）**：将 `re.compile(_type_re)` 改为 `re.compile(_type_re, re.IGNORECASE)`。这使得正则表达式在匹配行类型时忽略大小写，使 `read serr 1 2` 能被识别为 `command` 类型而非抛出异常。

2. **`_get_tables_from_qdp_file()` 函数（第 309 行）**：将 `if v == "NO"` 改为 `if v.upper() == "NO"`。这使得数据行中的缺失值标记（`no`、`No` 等）也能被正确识别为 masked 值。

核心语义：QDP 格式规范本身是大小写不敏感的，修复的本质是让 Python 解析器遵守这一规范。

## 调用链分析

```
Table.read(format='ascii.qdp')
  └─> QDP.read()
        └─> _read_table_qdp()
              └─> _get_tables_from_qdp_file()
                    ├─> _get_lines_from_file()          # 读取原始行
                    ├─> _get_type_from_list_of_lines()  # 对每行调用 _line_type()
                    │     └─> _line_type()              # 正则匹配行类型 [fix 1 在此]
                    ├─> _interpret_err_lines()          # 根据 err_specs 生成列名
                    └─> (数据行解析循环)                 # [fix 2 在此，处理 "NO" 值]
```

数据流：原始文本行 → `_line_type()` 分类 → `command_lines` 累积命令 → 解析命令得到 `err_specs` → `_interpret_err_lines()` 生成列名 → 数据行解析填充 `current_rows` → 构建 `Table`

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 直接冗余：是 golden patch fix1 的逆操作（移除 re.IGNORECASE） |
| B | 🔴 必须替换 | 替换 | 直接冗余：与 A 完全相同的 diff |
| C | 🔴 必须替换 | 替换 | 直接冗余：是 golden patch fix2 的部分逆操作（移除 .lower()） |
| D | 🔴 必须替换 | 替换 | 直接冗余：与 C 完全相同的 diff |
| E | 🔴 必须替换 | 替换 | 直接冗余：与 A/B 完全相同的 diff |

全部5个 mutation 均为直接冗余（golden patch 的逆操作），且存在大量重复（A=B=E，C=D），必须全部替换。

语义浅层共 0 个，替换 floor(0/2) = 0 个语义浅层。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：
```diff
diff --git a/astropy/io/ascii/qdp.py b/astropy/io/ascii/qdp.py
index 5324dc81c..65ed15751 100644
--- a/astropy/io/ascii/qdp.py
+++ b/astropy/io/ascii/qdp.py
@@ -68,7 +68,7 @@ def _line_type(line, delimiter=None):
     _new_re = rf"NO({sep}NO)+"
     _data_re = rf"({_decimal_re}|NO|[-+]?nan)({sep}({_decimal_re}|NO|[-+]?nan))*)"
     _type_re = rf"^\s*((?P<command>{_command_re})|(?P<new>{_new_re})|(?P<data>{_data_re})?\s*(\!(?P<comment>.*))?\s*$"
-    _line_type_re = re.compile(_type_re, re.IGNORECASE)
+    _line_type_re = re.compile(_type_re)
     line = line.strip()
     if not line:
         return "comment"
```
**分类**：🔴 必须替换
**理由**：这是 golden patch 第一处修复（添加 `re.IGNORECASE`）的直接逆操作，等同于还原 bug。代码审查者一眼就能发现缺少了 `re.IGNORECASE`。

**最终 mutation**：
```diff
diff --git a/astropy/io/ascii/qdp.py b/astropy/io/ascii/qdp.py
index 5324dc81cc..78a7998a36 100644
--- a/astropy/io/ascii/qdp.py
+++ b/astropy/io/ascii/qdp.py
@@ -80,7 +80,7 @@ def _line_type(line, delimiter=None):
         if val is None:
             continue
         if type_ == "data":
-            return f"data,{len(val.split(sep=delimiter))}"
+            return f"data,{len(val.split(sep=delimiter)) + 1}"
         else:
             return type_
```
**变异语义**：在 `_line_type()` 函数返回数据行列数时，将实际列数加 1。这导致 `_get_type_from_list_of_lines()` 统计到的 `ncol` 比实际多 1，进而使 `_interpret_err_lines()` 在用户提供列名时因 `all_error_cols + len(names) != ncols` 而抛出 "Inconsistent number of input colnames"。即使不提供列名，也会导致生成的列名多出一个空列，最终在 `assert not np.any([c == "" for c in colnames])` 处失败。这个 off-by-one 隐藏在列数统计的返回路径上，不在主要的命令解析逻辑中，难以通过简单测试发现。

---

### Group B — 替换
**原 mutation**：
```diff
diff --git a/astropy/io/ascii/qdp.py b/astropy/io/ascii/qdp.py
index 5324dc81c..65ed15751 100644
--- a/astropy/io/ascii/qdp.py
+++ b/astropy/io/ascii/qdp.py
@@ -68,7 +68,7 @@ def _line_type(line, delimiter=None):
-    _line_type_re = re.compile(_type_re, re.IGNORECASE)
+    _line_type_re = re.compile(_type_re)
```
**分类**：🔴 必须替换（与 A 完全相同）
**理由**：与 Group A 的 mutation 完全相同，重复且冗余。

**最终 mutation**：
```diff
diff --git a/astropy/io/ascii/qdp.py b/astropy/io/ascii/qdp.py
index 5324dc81cc..017da46bb9 100644
--- a/astropy/io/ascii/qdp.py
+++ b/astropy/io/ascii/qdp.py
@@ -202,7 +202,7 @@ def _interpret_err_lines(err_specs, ncols, names=None):
         terr_cols = err_specs.pop("terr", [])
 
     if names is not None:
-        all_error_cols = len(serr_cols) + len(terr_cols) * 2
+        all_error_cols = len(serr_cols) + len(terr_cols)
         if all_error_cols + len(names) != ncols:
             raise ValueError("Inconsistent number of input colnames")
```
**变异语义**：在 `_interpret_err_lines()` 中，计算误差列总数时将 `terr`（两侧误差）的贡献从 `* 2` 改为 `* 1`。`TERR` 命令实际会产生两列（`_perr` 和 `_nerr`），但此 mutation 认为只产生一列，导致列数验证通过但实际列名分配错误：`_nerr` 列不会被命名，触发 `assert not np.any([c == "" for c in colnames])` 失败。这个错误发生在深层辅助函数内，模拟了开发者对 `READ TERR` 语义（产生两列而非一列）的误解。

---

### Group C — 替换
**原 mutation**：
```diff
diff --git a/astropy/io/ascii/qdp.py b/astropy/io/ascii/qdp.py
index 5324dc81c..d84adfb97 100644
--- a/astropy/io/ascii/qdp.py
+++ b/astropy/io/ascii/qdp.py
@@ -297,7 +297,7 @@ def _get_tables_from_qdp_file(qdp_file, input_colnames=None, delimiter=None):
                     if len(command) < 3:
                         continue
-                    err_specs[command[1].lower()] = [int(c) for c in command[2:]]
+                    err_specs[command[1]] = [int(c) for c in command[2:]]
```
**分类**：🔴 必须替换
**理由**：是 golden patch 中 `command[1].lower()` 修复的直接逆操作。移除 `.lower()` 后，小写命令的键（如 `"serr"`）无法匹配 `_interpret_err_lines()` 中 `err_specs.pop("serr", [])` 期望的小写键，导致错误列名生成。

**最终 mutation**：
```diff
diff --git a/astropy/io/ascii/qdp.py b/astropy/io/ascii/qdp.py
index 5324dc81cc..b6c0e33f34 100644
--- a/astropy/io/ascii/qdp.py
+++ b/astropy/io/ascii/qdp.py
@@ -297,7 +297,7 @@ def _get_tables_from_qdp_file(qdp_file, input_colnames=None, delimiter=None):
                     # This should never happen, but just in case.
                     if len(command) < 3:
                         continue
-                    err_specs[command[1].lower()] = [int(c) for c in command[2:]]
+                    err_specs[command[1].lower()] = [int(c) for c in command[2:-1]]
             if colnames is None:
                 colnames = _interpret_err_lines(err_specs, ncol, names=input_colnames)
```
**变异语义**：在解析 `READ SERR`/`READ TERR` 命令时，将列号列表从 `command[2:]` 改为 `command[2:-1]`，截去最后一个列号。对于 `READ SERR 1 2`，只存储 `[1]` 而非 `[1, 2]`；对于 `READ SERR 1`（单列），存储 `[]` 即空列表，相当于没有误差列。这导致列名生成时错误地忽略了最后一个误差列，使测试中依赖特定列名（如 `Rate_err`）的断言失败。对于单列命令，所有误差信息都会静默丢失，行为与无误差列的表相同，非常隐蔽。

---

### Group D — 替换
**原 mutation**：
```diff
diff --git a/astropy/io/ascii/qdp.py b/astropy/io/ascii/qdp.py
index 5324dc81c..d84adfb97 100644
--- a/astropy/io/ascii/qdp.py
+++ b/astropy/io/ascii/qdp.py
@@ -297,7 +297,7 @@ def _get_tables_from_qdp_file(qdp_file, input_colnames=None, delimiter=None):
-                    err_specs[command[1].lower()] = [int(c) for c in command[2:]]
+                    err_specs[command[1]] = [int(c) for c in command[2:]]
```
**分类**：🔴 必须替换（与 C 完全相同）
**理由**：与 Group C 的 mutation 完全相同，重复且冗余。

**最终 mutation**：
```diff
diff --git a/astropy/io/ascii/qdp.py b/astropy/io/ascii/qdp.py
index 5324dc81cc..8869614cf8 100644
--- a/astropy/io/ascii/qdp.py
+++ b/astropy/io/ascii/qdp.py
@@ -208,7 +208,7 @@ def _interpret_err_lines(err_specs, ncols, names=None):
 
     shift = 0
     for i in range(ncols):
-        col_num = i + 1 - shift
+        col_num = i - shift
         if colnames[i] != "":
             continue
```
**变异语义**：在 `_interpret_err_lines()` 的列名分配循环中，将列编号从 `i + 1 - shift`（1-indexed）改为 `i - shift`（0-indexed）。这使得所有列的编号都偏移 -1：第 1 列变成第 0 列（不在 `serr_cols` 或 `terr_cols` 中），第 2 列变成第 1 列，等等。结果是误差列的归属全部错位——原本属于第 1 列的误差会被错误地关联到第 0 列（不存在），原本属于第 2 列的误差会被关联到第 1 列。这个 off-by-one 发生在内层循环的索引计算中，模拟了开发者将 1-indexed 的 QDP 列号误理解为 0-indexed 的错误，非常真实且难以察觉。

---

### Group E — 替换
**原 mutation**：
```diff
diff --git a/astropy/io/ascii/qdp.py b/astropy/io/ascii/qdp.py
index 5324dc81c..65ed15751 100644
--- a/astropy/io/ascii/qdp.py
+++ b/astropy/io/ascii/qdp.py
@@ -68,7 +68,7 @@ def _line_type(line, delimiter=None):
-    _line_type_re = re.compile(_type_re, re.IGNORECASE)
+    _line_type_re = re.compile(_type_re)
```
**分类**：🔴 必须替换（与 A/B 完全相同）
**理由**：与 Group A/B 的 mutation 完全相同，三组重复同一个 diff，质量极差。

**最终 mutation**：
```diff
diff --git a/astropy/io/ascii/qdp.py b/astropy/io/ascii/qdp.py
index 5324dc81cc..19f0574aad 100644
--- a/astropy/io/ascii/qdp.py
+++ b/astropy/io/ascii/qdp.py
@@ -286,7 +286,7 @@ def _get_tables_from_qdp_file(qdp_file, input_colnames=None, delimiter=None):
                     "This file contains multiple command blocks. Please verify",
                     AstropyUserWarning,
                 )
-            command_lines += line + "\n"
+            command_lines = line + "\n"
             continue
 
         if datatype.startswith("data"):
```
**变异语义**：在 `_get_tables_from_qdp_file()` 中，将命令行的累积方式从 `+=`（追加）改为 `=`（覆盖）。这意味着当文件中存在多条命令行时（如 `READ TERR 1` 和 `READ SERR 2` 分两行写），只有最后一条命令被保留，前面的命令全部丢失。对于只有单条命令的 QDP 文件（测试中常见），行为完全正常；只有当文件包含多个命令行时才会失败，例如同时有 `READ TERR` 和 `READ SERR` 的文件。这是一个典型的状态累积 bug，模拟了开发者误用赋值而非追加赋值的真实错误，难以通过只包含单条命令的简单测试检测。

---

## 新设计 Mutation 说明

### Mutation A（替换原 A）
**分析基础**：`_line_type()` 函数在匹配成功后，通过 `val.split(sep=delimiter)` 计算数据列数。这个列数会被传递给 `_get_type_from_list_of_lines()` 作为 `ncol`，再传给 `_interpret_err_lines()` 用于列名验证和生成。
**选择位置**：在返回列数时加 1，影响整个下游的列数计算。
**模拟的真实错误**：开发者可能误以为 `len(split)` 需要加 1 来得到"列数"（混淆了 0-indexed 和计数的概念）。

### Mutation B（替换原 B）
**分析基础**：`_interpret_err_lines()` 中，`READ TERR N` 命令实际为数据列 N 添加两列（正误差和负误差），所以误差列总数应为 `len(terr_cols) * 2`。
**选择位置**：改变 TERR 列数的计算系数，影响列数验证逻辑。
**模拟的真实错误**：开发者可能误以为 TERR 只产生一列误差（类似 SERR），这是对 QDP 格式规范的语义误解。

### Mutation C（替换原 C）
**分析基础**：`command[2:]` 提取所有列号，对于 `READ SERR 1 2`，得到 `['1', '2']`。`command[2:-1]` 截去最后一个，对于单列命令完全清空列表。
**选择位置**：命令解析时的切片操作，影响 err_specs 的内容。
**模拟的真实错误**：开发者可能误以为 `command[2:-1]` 是"去掉末尾可能的空字符串"的防御性写法，实际上 split() 不会产生末尾空串。

### Mutation D（替换原 D）
**分析基础**：`_interpret_err_lines()` 使用 1-indexed 的列号（与 QDP 格式一致），循环变量 `i` 是 0-indexed，所以 `col_num = i + 1 - shift` 做了正确的转换。
**选择位置**：循环内的索引转换，影响所有误差列的归属判断。
**模拟的真实错误**：Python 程序员习惯 0-indexed，容易忘记 QDP 列号从 1 开始，直接用 `i - shift` 是典型的 off-by-one 错误。

### Mutation E（替换原 E）
**分析基础**：`command_lines` 需要累积所有命令行（可能有多个 READ 命令），使用 `+=` 追加。
**选择位置**：命令行累积的赋值操作。
**模拟的真实错误**：开发者可能不注意到 `+=` 和 `=` 的区别，或者认为每次遇到命令都应该"重置"命令缓冲区，这在多命令文件中会静默丢失前面的命令。
