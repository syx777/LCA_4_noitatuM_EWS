# astropy__astropy-14182

## 问题背景

用户希望在 RST（reStructuredText）格式的 ASCII 表格输出中支持 `header_rows` 参数（如 `["name", "unit", "dtype"]`），以便在表头中同时输出列名、单位和数据类型等多行信息。在修复之前，`RST.__init__()` 不接受 `header_rows` 参数，导致 `TypeError`。此外，读取含多行表头的 RST 表格时，数据起始行号也是硬编码为 3，无法适配多行表头场景。

## Golden Patch 语义分析

Golden patch 做了以下四处修改：

1. **删除 `SimpleRSTData.start_line = 3`**：原来硬编码数据从第 3 行开始（适用于只有一行表头的 RST 格式），删除后该属性继承自 `FixedWidthData`（值为 `None`），由 `FixedWidth.__init__` 动态设置。

2. **`RST.__init__` 接受 `header_rows` 并传递给父类**：`FixedWidth.__init__` 中的逻辑会将 `header_rows` 存入 `self.header.header_rows` 和 `self.data.header_rows`，并在 `data.start_line is None` 时设置 `data.start_line = len(header_rows)`（但 RST 需要更大的值，由 `read()` 覆盖）。

3. **`write()` 使用动态 `idx`**：原来 `lines[1]` 是在只有一行表头时的 `=====` 分隔符位置。修复后用 `idx = len(self.header.header_rows)` 动态计算：`super().write()` 输出的 `lines` 顺序为 `[header_row_0, header_row_1, ..., =====, data_row_0, ...]`，所以 `lines[idx]` 恰好是 `=====` 分隔符行。

4. **新增 `read()` 方法**：RST 格式的数据起始行号为 `2 + len(header_rows)`（1 个前置 `=====` + N 个表头行 + 1 个后置 `=====`），需要在每次读取时动态设置，而不能在 `__init__` 中一次性设置（因为 `FixedWidth.__init__` 的条件赋值 `if self.data.start_line is None` 在 `start_line = 3` 被删除后才会执行）。

## 调用链分析

```
RST.read(table)
  └── self.data.start_line = 2 + len(self.header.header_rows)  # 动态设置
  └── FixedWidth.read(table) → BaseReader.read(table)
        └── self.data.get_data_lines(lines)  # 使用 data.start_line 切片
        └── self.header.get_cols(lines)       # 使用 header.start_line=1, header_rows 解析多行表头

RST.write(lines)
  └── FixedWidth.write(lines) → FixedWidthData.write(lines)
        └── 输出: [header_rows..., ===== (position_line), data_rows...]
  └── idx = len(self.header.header_rows)  # lines[idx] = ===== 分隔符
  └── 包装: [lines[idx]] + lines + [lines[idx]]
```

关键类属性：
- `SimpleRSTHeader.position_line = 0`：`=====` 行在输入中位于第 0 行
- `SimpleRSTHeader.start_line = 1`：表头名称行从第 1 行开始
- `SimpleRSTData.end_line = -1`：数据在倒数第 1 行（即末尾 `=====`）之前结束

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| B | 🟡 语义浅层 | 保留 | `- 1` 使 idx 偏移，位于关键的分隔符索引计算处，模拟真实边界错误 |
| C | 🔴 必须替换 | 替换 | `header_rows` 参数被接受但未传给 super，等价于还原原始 bug |
| D | 🔴 必须替换 | 替换 | 与 C 完全相同的 diff，重复 mutation |
| E | 🔴 必须替换 | 替换 | 引入 `strict_header_rows` 假标志，明显人工痕迹，不符合代码风格 |

语义浅层共 1 个（B），替换其中最弱的 floor(1/2) = 0 个：无需替换语义浅层。

必须替换 3 个（C、D、E）。

## 各组 Mutation 分析

### Group B — 保留

**原 mutation**：
```diff
diff --git a/astropy/io/ascii/rst.py b/astropy/io/ascii/rst.py
index d765e369e..0ec1cb9a6 100644
--- a/astropy/io/ascii/rst.py
+++ b/astropy/io/ascii/rst.py
@@ -78,7 +78,7 @@ class RST(FixedWidth):
 
     def write(self, lines):
         lines = super().write(lines)
-        idx = len(self.header.header_rows)
+        idx = len(self.header.header_rows) - 1
         lines = [lines[idx]] + lines + [lines[idx]]
         return lines
```

**分类**：🟡 语义浅层（保留）

**理由**：`- 1` 使 `idx` 偏移到 `=====` 分隔符的前一行（即最后一个表头内容行）。对于默认单行表头（`header_rows=["name"]`），`idx = 0`，`lines[0]` 是 name 行而非 `=====`，会产生错误的 RST 输出。虽然是单行符号修改，但位置处于分隔符行索引计算的关键节点，能模拟真实的 off-by-one 错误。

**最终 mutation**（与原相同）：
```diff
diff --git a/astropy/io/ascii/rst.py b/astropy/io/ascii/rst.py
index d765e369e..0ec1cb9a6 100644
--- a/astropy/io/ascii/rst.py
+++ b/astropy/io/ascii/rst.py
@@ -78,7 +78,7 @@ class RST(FixedWidth):
 
     def write(self, lines):
         lines = super().write(lines)
-        idx = len(self.header.header_rows)
+        idx = len(self.header.header_rows) - 1
         lines = [lines[idx]] + lines + [lines[idx]]
         return lines
```

**变异语义**：`write()` 使用错误的行作为 RST 表格的首尾分隔符（用最后一个表头内容行代替 `=====`），导致输出不是合法的 RST 表格。所有涉及 RST 写入的测试都会失败。

---

### Group C — 替换

**原 mutation**：
```diff
diff --git a/astropy/io/ascii/rst.py b/astropy/io/ascii/rst.py
index d765e369e..48451dfe3 100644
--- a/astropy/io/ascii/rst.py
+++ b/astropy/io/ascii/rst.py
@@ -74,7 +74,7 @@ class RST(FixedWidth):
     header_class = SimpleRSTHeader
 
     def __init__(self, header_rows=None):
-        super().__init__(delimiter_pad=None, bookend=False, header_rows=header_rows)
+        super().__init__(delimiter_pad=None, bookend=False)
```

**分类**：🔴 必须替换

**理由**：`__init__` 接受 `header_rows` 参数但不传给 `super()`，等价于直接还原原始 bug（`header_rows` 被忽略，`self.header.header_rows` 始终为 `["name"]`）。这是对 golden patch 最关键修改的直接逆操作，属于"功能等价冗余"。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/io/ascii/rst.py b/astropy/io/ascii/rst.py
index d765e369e3..1b8593e516 100644
--- a/astropy/io/ascii/rst.py
+++ b/astropy/io/ascii/rst.py
@@ -78,8 +78,7 @@ class RST(FixedWidth):
 
     def write(self, lines):
         lines = super().write(lines)
-        idx = len(self.header.header_rows)
-        lines = [lines[idx]] + lines + [lines[idx]]
+        lines = [lines[1]] + lines + [lines[1]]
         return lines
 
     def read(self, table):
```

**变异语义**：`write()` 使用硬编码的 `lines[1]` 而非动态计算的 `lines[idx]`。对于默认单行表头（`idx=1`），`lines[1]` 恰好等于 `lines[idx]`，行为正确，通过所有现有 P2P 测试。对于 3 行表头（`idx=3`），`lines[1]` 是 unit 行而非 `=====` 分隔符，输出的 RST 格式错误，`test_rst_with_header_rows` 的写入验证失败。模拟了开发者将原来的 `lines[1]` 改为动态 `idx` 时遗漏了修改，或错误地认为 `lines[1]` 总是分隔符行。

---

### Group D — 替换

**原 mutation**：
```diff
diff --git a/astropy/io/ascii/rst.py b/astropy/io/ascii/rst.py
index d765e369e..48451dfe3 100644
--- a/astropy/io/ascii/rst.py
+++ b/astropy/io/ascii/rst.py
@@ -74,7 +74,7 @@ class RST(FixedWidth):
     header_class = SimpleRSTHeader
 
     def __init__(self, header_rows=None):
-        super().__init__(delimiter_pad=None, bookend=False, header_rows=header_rows)
+        super().__init__(delimiter_pad=None, bookend=False)
```

**分类**：🔴 必须替换

**理由**：与 Group C 的 diff 完全相同，属于重复 mutation，必须替换。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/io/ascii/rst.py b/astropy/io/ascii/rst.py
index d765e369e3..e148437c44 100644
--- a/astropy/io/ascii/rst.py
+++ b/astropy/io/ascii/rst.py
@@ -27,6 +27,7 @@ class SimpleRSTHeader(FixedWidthHeader):
 
 
 class SimpleRSTData(FixedWidthData):
+    start_line = 3
     end_line = -1
     splitter_class = FixedWidthTwoLineDataSplitter
 
@@ -82,6 +83,3 @@ class RST(FixedWidth):
         lines = [lines[idx]] + lines + [lines[idx]]
         return lines
 
-    def read(self, table):
-        self.data.start_line = 2 + len(self.header.header_rows)
-        return super().read(table)
```

**变异语义**：在 `SimpleRSTData` 中恢复硬编码的 `start_line = 3`，同时删除 `RST.read()` 覆盖方法。对于默认单行表头：`FixedWidth.__init__` 检测到 `data.start_line is not None`（值为 3），不覆盖，数据从第 3 行开始读取，与原始行为一致，通过所有现有 P2P 测试。对于 3 行表头：`FixedWidth.__init__` 设置 `data.start_line = 3`（`len(["name","unit","dtype"])`），但正确值应为 5（1 前置 `=====` + 3 表头行 + 1 后置 `=====`），导致将 unit 和 dtype 行误读为数据行，`test_rst_with_header_rows` 读取验证失败。模拟了开发者修复了 `__init__` 和 `write()` 但忘记修复读取路径的错误。

---

### Group E — 替换

**原 mutation**：
```diff
diff --git a/astropy/io/ascii/rst.py b/astropy/io/ascii/rst.py
index d765e369e..49a6a3f4a 100644
--- a/astropy/io/ascii/rst.py
+++ b/astropy/io/ascii/rst.py
@@ -73,8 +73,11 @@ class RST(FixedWidth):
     data_class = SimpleRSTData
     header_class = SimpleRSTHeader
 
-    def __init__(self, header_rows=None):
-        super().__init__(delimiter_pad=None, bookend=False, header_rows=header_rows)
+    def __init__(self, header_rows=None, strict_header_rows=False):
+        if strict_header_rows:
+            super().__init__(delimiter_pad=None, bookend=False, header_rows=header_rows)
+        else:
+            super().__init__(delimiter_pad=None, bookend=False, header_rows=None)
```

**分类**：🔴 必须替换

**理由**：引入了 `strict_header_rows=False` 假标志，默认分支总是传 `header_rows=None`，等价于还原 bug。代码风格不自然，明显是人工注入的伪条件，代码审查中会立即被识别。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/io/ascii/rst.py b/astropy/io/ascii/rst.py
index d765e369e3..bb5a6b7848 100644
--- a/astropy/io/ascii/rst.py
+++ b/astropy/io/ascii/rst.py
@@ -74,7 +74,8 @@ class RST(FixedWidth):
     header_class = SimpleRSTHeader
 
     def __init__(self, header_rows=None):
-        super().__init__(delimiter_pad=None, bookend=False, header_rows=header_rows)
+        super().__init__(delimiter_pad=None, bookend=False,
+                         header_rows=list(reversed(header_rows)) if header_rows else header_rows)
```

**变异语义**：在 `__init__` 中将 `header_rows` 列表反转后传给 `super()`。当 `header_rows=None` 或 `header_rows=["name"]`（单元素列表，反转后不变）时，行为与 golden patch 完全一致，通过所有现有 P2P 测试。当 `header_rows=["name","unit","dtype"]` 时，实际传入的是 `["dtype","unit","name"]`，导致：写入时行顺序颠倒（dtype 行在最上，name 行在最下），读取时列名从 dtype 行解析（得到错误的列名如 `float64`），`test_rst_with_header_rows` 的读写验证均失败。模拟了开发者误解 `header_rows` 参数顺序语义的错误。

---

## 新设计 Mutation 说明

### Group C 新设计

**基于分析**：`write()` 中 `super().write()` 输出的 `lines` 为 `[header_row_0, ..., header_row_{N-1}, =====, data_row_0, ...]`，`lines[idx]` 是 `=====` 行（`idx = N`）。原始代码（base_commit）使用 `lines[1]`，因为当时只有 1 行表头，`lines[1]` 恰好是 `=====`。

**选择理由**：将 `lines[idx]` 改回 `lines[1]` 模拟了开发者忘记将 `write()` 中的硬编码索引改为动态 `idx` 的错误。这是一个非常自然的遗漏：开发者可能只关注了 `__init__` 和 `read()` 的修复，而忽略了 `write()` 中的硬编码值。对于单行表头完全透明，只在多行表头场景下暴露。

### Group D 新设计

**基于分析**：golden patch 删除了 `SimpleRSTData.start_line = 3` 并添加了 `RST.read()` 来动态设置 `start_line`。这两个改动是配套的：删除硬编码是为了让动态计算生效；如果只删除硬编码而不添加 `read()`，`FixedWidth.__init__` 会将 `start_line` 设为 `len(header_rows)`（不正确）；如果只添加 `read()` 而不删除硬编码，`FixedWidth.__init__` 检测到 `start_line is not None` 不会覆盖，`read()` 再覆盖为正确值（功能正确但冗余）。

**选择理由**：恢复 `start_line = 3` 并删除 `read()` 模拟了开发者只修复了 `__init__` 和 `write()` 但忘记修复读取路径的错误。对于默认单行表头，`start_line = 3` 仍然正确（因为 `FixedWidth.__init__` 检测到非 None 不覆盖）；对于多行表头，`FixedWidth.__init__` 将 `start_line` 设为 `len(header_rows) = 3`，但正确值为 5，导致读取错误。

### Group E 新设计

**基于分析**：`FixedWidth.__init__` 中 `header_rows` 的顺序决定了 `FixedWidthHeader.get_cols()` 如何解析各行（`header_rows.index("name")` 找 name 行，其他属性按顺序读取）。`FixedWidthData.write()` 也按 `header_rows` 顺序输出各行。

**选择理由**：将 `header_rows` 反转后传给 `super()` 模拟了开发者误解参数语义（认为最后指定的是"主要"行，应该先处理）的错误。对于 `None` 或单元素列表完全透明，只在多元素 `header_rows` 场景下暴露。读写路径均受影响，但错误表现不同（写入顺序颠倒，读取列名错误），使得 bug 更难被简单测试捕获。
