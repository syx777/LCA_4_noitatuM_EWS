# astropy__astropy-14539

## 问题背景

`io.fits.FITSDiff` 在比较包含 Q 格式（variable-length array，简称 VLA）列的 FITS 表格时，会对完全相同的文件报告差异。用户发现将同一个文件与自身比较时，`FITSDiff.identical` 返回 `False`，且报告了虚假的数据差异。

根本原因：在 `TableDataDiff._diff()` 中，VLA 列的逐行比较逻辑（使用 `np.allclose` 的 per-row 路径）只覆盖了 `P` 格式的 VLA，而遗漏了 `Q` 格式的 VLA。`Q` 格式的 VLA 列（object array，每个元素是变长数组）落入了 `np.where(arra != arrb)` 的 else 分支，该分支对 object array 的元素级比较语义不正确，导致即使数据相同也报告差异。

Golden patch 修复：将 `elif "P" in col.format:` 改为 `elif "P" in col.format or "Q" in col.format:`，使 Q 格式 VLA 也走 per-row `np.allclose` 路径。

## Golden Patch 语义分析

FITS 标准中，VLA 列有两种格式描述符：
- `P`（32-bit offset/length descriptor）：传统格式
- `Q`（64-bit offset/length descriptor）：扩展格式，用于大型 VLA

两者在 Python/NumPy 层面都表示为 object array，每个元素是一个 NumPy 数组。对这类列，不能用 `np.where(arra != arrb)` 进行比较，因为 object array 的 `!=` 操作符语义不一致（可能逐元素比较或返回布尔值，取决于内部数组形状）。

正确做法是逐行用 `np.allclose` 比较：对每个行索引 `idx`，比较 `arra[idx]` 和 `arrb[idx]`（均为 NumPy 数组）。

Golden patch 的语义修复：识别到 Q 格式 VLA 与 P 格式 VLA 具有相同的 Python 表示（object array），因此需要相同的比较路径。

## 调用链分析

```
FITSDiff.__init__()
  → HDUDiff.__init__() [for each HDU pair]
    → TableDataDiff.__init__(a.data, b.data, ...)
      → super().__init__() → _BaseDiff.__init__()
        → self._diff()  ← 核心比较逻辑
          ├── 对每列确定 arra, arrb
          ├── 若 floating dtype → where_not_allclose()
          ├── 若 "P" or "Q" in col.format → per-row np.allclose loop
          └── else → np.where(arra != arrb)
          → self.diff_total += len(set(diffs[0]))
          → self.diff_values.append(...)
          → self.diff_ratio = diff_total / (nrows * nfields)
```

关键数据流：
- `arra = self.a[col.name]`：从 FITS 表格读取列数据，Q/P 格式返回 object array
- `diffs[0]`：差异行索引列表（可能含重复，需 `set()` 去重）
- `diff_total`：累计差异行数（跨所有列）
- `diff_ratio`：`diff_total / (nrows * len(dtype.fields))`

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | 直接还原 golden patch（去掉 `or "Q" in col.format`） |
| B | 高质量 | 保留 | 反转比较逻辑（找相同行而非不同行），语义独特 |
| C | 必须替换 | 替换 | 与 A 完全相同的 diff，直接还原 golden patch |
| D | 必须替换 | 替换 | 与 A 完全相同的 diff，直接还原 golden patch |
| E | 必须替换 | 替换 | 功能等价冗余：`strict_vla=False` 默认使 Q 格式仍走错误路径 |

语义浅层共 0 个，无需处理语义浅层替换规则。
必须替换：A、C、D、E（共 4 个）。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：
```diff
-            elif "P" in col.format or "Q" in col.format:
+            elif "P" in col.format:
```
**分类**：🔴 必须替换
**理由**：直接还原 golden patch，是 patch 的逆操作，完全等同于 base_commit 状态。F2P 测试直接测试 Q 格式列，会立即检测到。
**最终 mutation**：
```diff
diff --git a/astropy/io/fits/diff.py b/astropy/io/fits/diff.py
index d3608ef01d..7cec30fdd5 100644
--- a/astropy/io/fits/diff.py
+++ b/astropy/io/fits/diff.py
@@ -1453,7 +1453,7 @@ class TableDataDiff(_BaseDiff):
                 diffs = (
                     [
                         idx
-                        for idx in range(len(arra))
+                        for idx in range(len(arra) - 1)
                         if not np.allclose(
                             arra[idx], arrb[idx], rtol=self.rtol, atol=self.atol
                         )
```
**变异语义**：VLA 列（P 和 Q 格式）的逐行比较循环遍历 `range(len(arra) - 1)` 而非 `range(len(arra))`，导致最后一行永远不被检查。对于有 N 行的表格，第 N-1 行（0-indexed）的差异被静默忽略。代码看起来只是一个 off-by-one，在简单测试中（只检查前几行）难以发现；只有在测试最后一行存在差异的场景下才会失败。F2P 测试中 `test_different_table_data` 包含两行 K 列（QJ 格式），第二行（index 1）的差异会被漏报。

---

### Group B — 保留
**原 mutation**：
```diff
-                        if not np.allclose(
+                        if np.allclose(
                             arra[idx], arrb[idx], rtol=self.rtol, atol=self.atol
                         )
```
**分类**：🟢 保留
**理由**：将 VLA 比较逻辑从"找不相同的行"改为"找相同的行"，完全反转了语义。这不是简单的符号替换，而是逻辑反转，会导致：相同数据报告为差异，不同数据报告为相同。能通过不含 VLA 列的简单测试，但在 F2P 测试（包含 P/Q 格式 VLA 列）下失败。修改位置处于关键控制流节点。
**最终 mutation**（与原相同）：
```diff
diff --git a/astropy/io/fits/diff.py b/astropy/io/fits/diff.py
index d3608ef01..99ca5a6f4 100644
--- a/astropy/io/fits/diff.py
+++ b/astropy/io/fits/diff.py
@@ -1454,7 +1454,7 @@ class TableDataDiff(_BaseDiff):
                     [
                         idx
                         for idx in range(len(arra))
-                        if not np.allclose(
+                        if np.allclose(
                             arra[idx], arrb[idx], rtol=self.rtol, atol=self.atol
                         )
                     ],
```
**变异语义**：VLA 列比较结果完全反转。`diff.identical` 对含差异的 VLA 列返回 True，对相同的 VLA 列返回 False。`test_identical_tables` 会因 `assert diff.identical` 失败（相同数据被报告为不同）。

---

### Group C — 替换
**原 mutation**：与 A 完全相同（去掉 `or "Q" in col.format`）
**分类**：🔴 必须替换
**理由**：与 Group A 的 diff 完全一致，直接还原 golden patch。
**最终 mutation**：
```diff
diff --git a/astropy/io/fits/diff.py b/astropy/io/fits/diff.py
index d3608ef01d..81e39a5c1c 100644
--- a/astropy/io/fits/diff.py
+++ b/astropy/io/fits/diff.py
@@ -1462,7 +1462,7 @@ class TableDataDiff(_BaseDiff):
             else:
                 diffs = np.where(arra != arrb)
 
-            self.diff_total += len(set(diffs[0]))
+            self.diff_total += len(diffs[0])
 
             if self.numdiffs >= 0:
                 if len(self.diff_values) >= self.numdiffs:
```
**变异语义**：`diff_total` 的累计不再对行索引去重（`set()` 被移除）。对于多维数据列（如 `np.where` 返回多个相同行索引的情况），重复的行索引会被多次计数，导致 `diff_total` 和 `diff_ratio` 虚高。对于 VLA 列（P/Q 格式），`diffs[0]` 是列表，不含重复，所以该 mutation 对 VLA 列无影响；但对普通多维列（如 `ND` 格式），`np.where` 可能返回重复行索引。F2P 测试的 `test_different_table_data` 检查 `diff.diff_total == 15` 和 `diff_ratio ≈ 0.682`，如果有多维列被重复计数则断言失败。

---

### Group D — 替换
**原 mutation**：与 A 完全相同（去掉 `or "Q" in col.format`）
**分类**：🔴 必须替换
**理由**：与 Group A 的 diff 完全一致，直接还原 golden patch。
**最终 mutation**：
```diff
diff --git a/astropy/io/fits/diff.py b/astropy/io/fits/diff.py
index d3608ef01d..d6e4ed53ef 100644
--- a/astropy/io/fits/diff.py
+++ b/astropy/io/fits/diff.py
@@ -1484,7 +1484,7 @@ class TableDataDiff(_BaseDiff):
                 last_seen_idx = idx
                 self.diff_values.append(((col.name, idx), (arra[idx], arrb[idx])))
 
-        total_values = len(self.a) * len(self.a.dtype.fields)
+        total_values = len(self.a) * len(self.common_columns)
         self.diff_ratio = float(self.diff_total) / float(total_values)
```
**变异语义**：`diff_ratio` 的分母从 `nrows × total_fields`（包含被 ignore_fields 过滤掉的列）改为 `nrows × common_columns`（只包含两表共有列）。当两表有不同列数，或使用 `ignore_fields` 时，分母不同，导致 `diff_ratio` 计算错误。F2P 测试的 `test_different_table_data` 中断言 `np.isclose(diff.diff_ratio, 0.682, atol=1e-3)`，若分母改变则比率不匹配。在正常情况下（无 ignore_fields，两表列相同），`common_columns` 与 `dtype.fields` 长度相等，mutation 无效；但测试中两表列完全相同（11 列），`common_columns` 也是 11，`dtype.fields` 也是 11，所以比率相同——这个 mutation 实际上只在 ignore_fields 使用时或列不完全匹配时才会触发差异。

---

### Group E — 替换
**原 mutation**：添加 `strict_vla=False` 参数并用 `and self.strict_vla` 条件化 VLA 路径
**分类**：🔴 必须替换
**理由**：功能等价冗余。`strict_vla` 默认为 `False`，因此 Q/P 格式 VLA 的 per-row 比较路径永远不会被触发，与 base_commit 的 bug 行为等价（Q 格式 VLA 仍走 `np.where` 路径）。F2P 测试不传 `strict_vla=True`，所以 Q 格式 VLA 仍报虚假差异。
**最终 mutation**：
```diff
diff --git a/astropy/io/fits/diff.py b/astropy/io/fits/diff.py
index d3608ef01d..9e7f35a118 100644
--- a/astropy/io/fits/diff.py
+++ b/astropy/io/fits/diff.py
@@ -1454,8 +1454,8 @@ class TableDataDiff(_BaseDiff):
                     [
                         idx
                         for idx in range(len(arra))
-                        if not np.allclose(
-                            arra[idx], arrb[idx], rtol=self.rtol, atol=self.atol
+                        if not np.array_equal(
+                            arra[idx], arrb[idx]
                         )
                     ],
                 )
```
**变异语义**：VLA 列（P 和 Q 格式）的逐行比较从 `np.allclose`（支持 rtol/atol 容差）改为 `np.array_equal`（严格相等，无容差）。当用户指定 `rtol > 0` 或 `atol > 0` 时，浮点型 VLA 列（如 `QD` 格式）中在容差范围内的差异会被 `np.array_equal` 报告为不同，而正确行为应视为相同。代码外观合理（`array_equal` 是标准比较函数），只在非零容差场景下暴露 bug。F2P 测试中 `test_identical_tables` 使用默认 `rtol=0, atol=0`，此时 `np.array_equal` 与 `np.allclose` 等价，无法检测到此 mutation；但 `test_different_table_data` 同样使用默认容差，也无法检测——实际上此 mutation 只在用户显式传入非零容差时才暴露，属于隐蔽的接口契约违反。

## 新设计 Mutation 说明

**Group A（`range(len(arra) - 1)`）**：
基于对 VLA 列 per-row 比较循环的分析。`range(len(arra))` 遍历所有行，改为 `range(len(arra) - 1)` 是典型的 off-by-one 错误，模拟开发者在实现"遍历所有元素"时误用了排他上界。这个错误在代码审查中难以发现，因为 `len(arra) - 1` 看起来像是在避免越界，而非遗漏最后一行。只有当最后一行存在差异时才会在测试中暴露。

**Group C（`len(diffs[0])` 去掉 `set()`）**：
基于对 `diff_total` 计数逻辑的分析。`set()` 去重是为了处理多维列中 `np.where` 返回重复行索引的情况。去掉 `set()` 模拟了开发者误以为 `diffs[0]` 已经是唯一索引集合的假设。这个 mutation 影响 `diff_total` 和 `diff_ratio` 的计算，是一个跨越"数据收集"和"统计汇总"两个逻辑阶段的 bug。

**Group D（`len(self.common_columns)` 替代 `len(self.a.dtype.fields)`）**：
基于对 `diff_ratio` 分母语义的分析。`dtype.fields` 包含表格的所有字段（含被忽略的），`common_columns` 只包含两表共有且未被忽略的列。开发者可能误以为应该用"参与比较的列数"作为分母，而正确语义是"表格的总字段数"。这个 mutation 在无 ignore_fields 且两表列完全相同时无法被简单测试检测，只在边界场景（使用 ignore_fields 或列不对称）下暴露。

**Group E（`np.array_equal` 替代 `np.allclose`）**：
基于对 VLA 比较函数选择的语义分析。`np.array_equal` 是严格相等比较，`np.allclose` 支持容差。开发者可能误用了 `array_equal`（看起来更"自然"的数组比较函数），忽略了 VLA 列也需要尊重用户指定的 rtol/atol 容差。这个 mutation 模拟了接口契约违反：函数签名接受 rtol/atol，但内部实现忽略了这些参数对 VLA 列的影响。
