# astropy__astropy-13453

## 问题背景

HTML 格式的 ASCII 表格写入器不支持用户通过 `formats=` 参数传入的列格式化字符串。用户期望 `table.write(sp, format="html", formats={"C1": "04d", "C2": ".2e"})` 能够将整数列格式化为 `"0001"`、浮点列格式化为 `"1.23e-11"` 等，但实际输出忽略了 `formats` 参数，直接使用默认的字符串转换。

## Golden Patch 语义分析

Golden patch 在 `HTML.write()` 方法中新增了两行：

```python
self.data.cols = cols          # 第一行：将列列表赋给 data 对象的 cols 属性
self.data._set_col_formats()   # 第二行：调用方法将 formats 字典中的格式字符串应用到各列
```

**修复逻辑**：

1. `_get_writer()` 在初始化 writer 时，会将用户传入的 `formats` 参数赋给 `writer.data.formats`（`core.py` 第 1726 行）。
2. `_set_col_formats()` 方法（`core.py` 第 934 行）遍历 `self.cols`，对每个列名在 `self.formats` 中的列设置 `col.info.format`。
3. 问题在于：`HTML.write()` 在 base_commit 状态下既没有设置 `self.data.cols`（所以 `_set_col_formats` 遍历的是空列表），也没有调用 `_set_col_formats()`（所以格式字符串根本不会被应用）。
4. 修复后，`col.info.format` 被正确设置，`col.info.iter_str_vals()` 在调用 `_pformat_col_iter` 时会读取 `col_format = col.info.format`，从而应用用户指定的格式。

关键点：`ColumnInfo.attrs_from_parent = attr_names`，意味着 `col.info.format` 实际上读写的是 `col._format`（存储在父对象上），这是 `iter_str_vals()` 所读取的格式属性。

## 调用链分析

```
table.write(sp, format="html", formats={"C1": "04d", "C2": ".2e"})
  └─ ui.write()
       ├─ _get_writer(Writer=HTML, formats=...) → writer.data.formats = {"C1": "04d", "C2": ".2e"}
       └─ writer.write(table) → HTML.write(table)
            ├─ self.data.cols = cols                     # [NEW] 设置列列表
            ├─ self.data._set_col_formats()              # [NEW] 遍历 self.data.cols，设置 col.info.format
            │    └─ col.info.format = self.formats[col.info.name]  # 对每个匹配列名的列设置格式
            └─ for col in cols:
                 └─ col.info.iter_str_vals()             # 读取 col.info.format → 应用格式
                      └─ _pformat_col_iter(col, ...)
                           └─ col_format = col.info.format  # 使用已设置的格式字符串
```

数据流：`formats` kwarg → `writer.data.formats` dict → `_set_col_formats()` → `col.info.format` → `iter_str_vals()` → 格式化字符串输出

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 含明显 `# Bug:` 注释，且先设空列表再恢复，人工痕迹明显 |
| B | 🔴 必须替换 | 替换 | 注释掉 `_set_col_formats()` 调用，代码审查中立即可见 |
| C | 🔴 必须替换 | 替换 | 注释掉 `self.data.cols = cols`，代码审查中立即可见 |
| D | 新增 | 新增 | 原数据中缺少 D 组，需新增一个高质量 mutation |
| E | 🔴 必须替换 | 替换 | 添加 `apply_formats=False` 参数并条件化调用，签名修改不自然 |

语义浅层共 0 个，无需选择性替换。全部 4 个为必须替换，另需新增 D 组。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：
```diff
@@ -349,13 +349,15 @@ class HTML(core.BaseReader):
         cols = list(table.columns.values())
 
         self.data.header.cols = cols
-        self.data.cols = cols
-
+        self.data.cols = []  # Bug: set to empty list instead of actual columns
+        
         if isinstance(self.data.fill_values, tuple):
             self.data.fill_values = [self.data.fill_values]
 
         self.data._set_fill_values(cols)
         self.data._set_col_formats()
+        
+        self.data.cols = cols  # Set to actual columns after _set_col_formats()
```
**分类**：🔴 必须替换
**理由**：含有明显的 `# Bug:` 注释和 `# Set to actual columns after _set_col_formats()` 注释，人工痕迹极其明显。代码审查者一眼就能看出这是故意引入的 bug。此外，先设空列表再恢复的模式在真实代码中没有任何合理解释。

**最终 mutation**：
```diff
diff --git a/astropy/io/ascii/html.py b/astropy/io/ascii/html.py
index c5a1105145..0790eff4e1 100644
--- a/astropy/io/ascii/html.py
+++ b/astropy/io/ascii/html.py
@@ -349,13 +349,13 @@ class HTML(core.BaseReader):
         cols = list(table.columns.values())
 
         self.data.header.cols = cols
-        self.data.cols = cols
 
         if isinstance(self.data.fill_values, tuple):
             self.data.fill_values = [self.data.fill_values]
 
         self.data._set_fill_values(cols)
         self.data._set_col_formats()
+        self.data.cols = cols
 
         lines = []
```
**变异语义**：`_set_col_formats()` 被调用时 `self.data.cols` 尚未赋值，因此遍历的是空列表（或上次调用遗留的旧状态），没有任何列的 `col.info.format` 被设置。格式字符串完全失效。这模拟了开发者添加两行修复时写错顺序的真实错误——认为 `cols` 参数会被 `_set_col_formats()` 直接使用，而不知道它实际读取 `self.cols` 属性。简单的功能测试（不涉及格式化）会通过，只有测试格式输出的测试会失败。

---

### Group B — 替换
**原 mutation**：
```diff
@@ -355,7 +355,7 @@ class HTML(core.BaseReader):
             self.data.fill_values = [self.data.fill_values]
 
         self.data._set_fill_values(cols)
-        self.data._set_col_formats()
+        # self.data._set_col_formats()
```
**分类**：🔴 必须替换
**理由**：注释掉代码调用是最明显的人工变异手法之一，代码审查中会立即被发现。任何 linter 或代码审查工具都会标记注释掉的代码。

**最终 mutation**：
```diff
diff --git a/astropy/io/ascii/core.py b/astropy/io/ascii/core.py
index 1a7785bd21..d960b73f92 100644
--- a/astropy/io/ascii/core.py
+++ b/astropy/io/ascii/core.py
@@ -1724,7 +1724,7 @@ def _get_writer(Writer, fast_writer, **kwargs):
         writer.header.splitter.quotechar = kwargs['quotechar']
         writer.data.splitter.quotechar = kwargs['quotechar']
     if 'formats' in kwargs:
-        writer.data.formats = kwargs['formats']
+        writer.header.formats = kwargs['formats']
     if 'strip_whitespace' in kwargs:
         if kwargs['strip_whitespace']:
             # Restore the default SplitterClass process_val method which strips
```
**变异语义**：`formats` 字典被错误地赋给 `writer.header.formats` 而非 `writer.data.formats`。`_set_col_formats()` 方法读取的是 `self.formats`（即 `self.data.formats`），后者保持为 `{}`，因此没有任何列的格式被设置。这模拟了开发者混淆 `header` 和 `data` 属性的真实错误——在其他地方（如 `delimiter`、`comment`、`quotechar`）都同时设置了 `header` 和 `data`，开发者可能误以为 `formats` 也应该设置在 `header` 上。变异代码完全合法，不会报错，只是格式不生效。

---

### Group C — 替换
**原 mutation**：
```diff
@@ -349,7 +349,7 @@ class HTML(core.BaseReader):
         cols = list(table.columns.values())
 
         self.data.header.cols = cols
-        self.data.cols = cols
+        # self.data.cols = cols
```
**分类**：🔴 必须替换
**理由**：注释掉代码是最明显的人工变异手法，与 B 组同理。

**最终 mutation**：
```diff
diff --git a/astropy/io/ascii/html.py b/astropy/io/ascii/html.py
index c5a1105145..0b62b9405a 100644
--- a/astropy/io/ascii/html.py
+++ b/astropy/io/ascii/html.py
@@ -355,6 +355,7 @@ class HTML(core.BaseReader):
             self.data.fill_values = [self.data.fill_values]
 
         self.data._set_fill_values(cols)
+        self.data.formats = {}
         self.data._set_col_formats()
 
         lines = []
```
**变异语义**：在调用 `_set_col_formats()` 之前，将 `self.data.formats` 清空为空字典。`_set_col_formats()` 遍历列时，`col.info.name in self.formats` 始终为 False，没有任何格式被应用。这模拟了开发者认为每次写入前应重置格式以避免状态污染的真实错误——一种"防御性编程"的过度应用。代码看起来合理，不会报错，简单的写入测试会通过，只有检查格式化输出的测试会失败。

---

### Group D — 新增
**原 mutation**：（原数据中缺少 D 组，此为新增）

**最终 mutation**：
```diff
diff --git a/astropy/io/ascii/html.py b/astropy/io/ascii/html.py
index c5a1105145..ac6b9b4bfc 100644
--- a/astropy/io/ascii/html.py
+++ b/astropy/io/ascii/html.py
@@ -439,7 +439,7 @@ class HTML(core.BaseReader):
                                     new_cols.append(new_col)
                             else:
 
-                                col_iter_str_vals = self.fill_values(col, col.info.iter_str_vals())
+                                col_iter_str_vals = self.fill_values(col, (str(v) for v in col))
                                 col_str_iters.append(col_iter_str_vals)
 
                                 new_cols_escaped.append(col_escaped)
```
**变异语义**：将 `col.info.iter_str_vals()` 替换为 `(str(v) for v in col)` 来生成列的字符串值。前者通过 `_pformat_col_iter` 读取 `col.info.format` 并应用格式字符串；后者直接使用 Python 内置的 `str()` 转换，完全绕过格式化机制。这模拟了开发者使用"更简单"的迭代方式的真实错误——两种写法都能产生字符串序列，但语义不同。`str(1)` = `"1"`（非 `"0001"`），`str(1.234e-11)` = `"1.234e-11"`（非 `"1.23e-11"`）。不涉及格式化的测试（如测试表格结构、HTML 标签等）会通过，只有检查格式化输出的测试会失败。

---

### Group E — 替换
**原 mutation**：
```diff
@@ -339,7 +339,7 @@ class HTML(core.BaseReader):
         self.outputter = HTMLOutputter()
         return super().read(table)
 
-    def write(self, table):
+    def write(self, table, apply_formats=False):
         """
         ...
         """
@@ -355,7 +355,8 @@ class HTML(core.BaseReader):
             self.data.fill_values = [self.data.fill_values]
 
         self.data._set_fill_values(cols)
-        self.data._set_col_formats()
+        if apply_formats:
+            self.data._set_col_formats()
```
**分类**：🔴 必须替换
**理由**：修改了 `write()` 方法的签名，添加了 `apply_formats=False` 参数。这是一个非常不自然的变异：公共 API 方法签名的改变会立即引起代码审查者的注意；默认值 `False` 意味着格式化默认关闭，这与用户通过 `formats=` 参数传入格式的意图完全矛盾；而且上层调用 `writer.write(table)` 不传 `apply_formats`，所以格式化永远不会生效。

**最终 mutation**：
```diff
diff --git a/astropy/io/ascii/core.py b/astropy/io/ascii/core.py
index 1a7785bd21..920f5e11bf 100644
--- a/astropy/io/ascii/core.py
+++ b/astropy/io/ascii/core.py
@@ -933,7 +933,7 @@ class BaseData:
 
     def _set_col_formats(self):
         """WRITE: set column formats."""
-        for col in self.cols:
+        for col in self.cols[1:]:
             if col.info.name in self.formats:
                 col.info.format = self.formats[col.info.name]
```
**变异语义**：`_set_col_formats()` 从 `self.cols[1:]` 开始迭代，跳过第一列。第一列（如 `C1`）的格式字符串不会被应用，而其余列（如 `C2`）仍然正常格式化。这模拟了开发者在修改迭代范围时引入的 off-by-one 错误——可能是在某个特殊场景下（如表格有索引列）尝试跳过第一列的错误泛化。`_set_col_formats()` 是基类 `BaseData` 的方法，影响所有使用该方法的 writer，但只有当第一列有格式要求时才会暴露。测试中 C1 需要 `"04d"` 格式，因此测试会失败；但如果用户只对非第一列指定格式，该 mutation 不会被检测到。

## 新设计 Mutation 说明

### A 组替换（顺序错误）
基于对 `_set_col_formats()` 实现的深度分析：该方法遍历 `self.cols` 而非接受参数，因此 `self.data.cols` 必须在调用前设置。将 `self.data.cols = cols` 移到 `_set_col_formats()` 调用之后，模拟了开发者不了解内部依赖关系、写错初始化顺序的真实错误。

### B 组替换（错误属性赋值）
基于对 `_get_writer()` 中 `kwargs` 处理逻辑的分析：`delimiter`、`comment`、`quotechar` 都同时设置在 `header` 和 `data` 上，而 `formats` 只设置在 `data` 上。将其改为 `header.formats` 模拟了开发者看到其他属性的双重赋值模式后，误以为 `formats` 也应该设置在 `header` 上的错误。

### C 组替换（防御性清空）
基于对 `HTML.write()` 中状态管理的分析：`self.data.formats` 在 `_get_writer()` 中被设置一次，在整个写入过程中应保持不变。在 `_set_col_formats()` 之前清空它，模拟了开发者误以为需要在每次写入时重置格式状态以避免跨调用污染的错误。

### D 组新增（绕过格式化迭代）
基于对 HTML 写入循环中 `col.info.iter_str_vals()` vs 直接迭代的分析：`iter_str_vals()` 是应用 `col.info.format` 的关键路径，而 `(str(v) for v in col)` 是更直观但不正确的替代方案。这是一个跨越 golden patch 修复点（`_set_col_formats` 的调用处）和实际使用点（`iter_str_vals` 的调用处）的变异，模拟了开发者知道需要设置格式但在使用时选择了错误迭代方法的真实错误。

### E 组替换（off-by-one 切片）
基于对 `_set_col_formats()` 作为 `BaseData` 基类方法的分析：该方法被所有 writer 共享，修改其迭代范围会影响所有格式化写入操作。`self.cols[1:]` 模拟了开发者在某种特殊场景（如跳过索引列）下引入的错误泛化，只有当第一列有格式要求时才会被检测到，使得该 mutation 在许多测试场景下难以发现。
