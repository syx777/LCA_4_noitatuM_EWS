# astropy__astropy-12907

## 问题背景

`astropy.modeling` 的 `separability_matrix` 函数在处理**嵌套 CompoundModel** 时，计算结果错误。

具体表现：对于 `rot & (sh1 & sh2)`（即 Rotation2D 与两个 Shift 的嵌套并联），期望的 separability 矩阵应该是：
```
[[True, True, False, False],
 [True, True, False, False],
 [False, False, True, False],
 [False, False, False, True]]
```
但实际返回：
```
[[True, True, False, False],
 [True, True, False, False],
 [False, False, True, True],  ← 错误：第3行最后两列应为 False
 [False, False, True, True]]  ← 错误：第4行最后两列应为 False
```
即嵌套的 `sh1 & sh2` 的独立性被错误地合并了。

## Golden Patch 语义分析

**修改位置**：`astropy/modeling/separable.py`，`_cstack()` 函数，第245行

**原始代码**：
```python
cright[-right.shape[0]:, -right.shape[1]:] = 1
```
**修复后**：
```python
cright[-right.shape[0]:, -right.shape[1]:] = right
```

**语义解释**：`_cstack` 实现 `&`（并联）操作符的坐标矩阵运算。当右操作数 `right` 是一个 ndarray（即已经由递归计算得到的子矩阵）时，代码错误地将其内容替换为常数 `1`，而不是保留 `right` 的实际值。

这导致任何嵌套的右子树（如 `sh1 & sh2`）的 separability 结构被抹去，全部变成"非独立"（全1），从而使嵌套的独立输出看起来相互依赖。

**根本原因**：在 base_commit 中，`_cstack` 对 Model 对象（叶节点）的处理是正确的（调用 `_coord_matrix`），但对 ndarray（来自递归的中间结果）的处理有 bug，用 `= 1` 代替了 `= right`。

## 调用链分析

```
separability_matrix(transform)
  └─ _separable(transform)
       ├─ [叶节点 Model] → _coord_matrix(transform, 'left', n_outputs)
       │                    返回 ndarray (0/1 矩阵)
       └─ [CompoundModel] → 递归计算:
            sepleft  = _separable(transform.left)   # 返回 ndarray
            sepright = _separable(transform.right)  # 返回 ndarray
            └─ _operators[transform.op](sepleft, sepright)
                 └─ [& 操作] → _cstack(sepleft, sepright)
                      ├─ sepleft 是 ndarray → 走 else 分支（ndarray 路径）
                      └─ sepright 是 ndarray → 走 else 分支（ndarray 路径）← BUG 在此

is_separable(transform)
  └─ _separable(transform)  # 同上
  └─ row_sums = separable_matrix.sum(1)
  └─ np.where(row_sums != 1, False, True)  # 判断每行是否恰好依赖1个输入
```

**关键发现**：`_cstack` 中的 `isinstance(left, Model)` 和 `isinstance(right, Model)` 分支**在正常的 `_separable` 调用流程中永远不会执行**，因为 `_separable` 总是返回 ndarray，传给 `_cstack` 的两个参数都是 ndarray。这两个 Model 分支是为直接调用 `_cstack` 保留的接口，但在实际的 separability 计算中，所有逻辑都走 `else`（ndarray）分支。

## 各组 Mutation 分析

### Group A — 替换（低质量）

**原 mutation**：
```diff
-    if (transform_matrix := transform._calculate_separability_matrix()) is not NotImplemented:
+    if (transform_matrix := transform._calculate_separability_matrix()) is NotImplemented:
```

**质量评估**：低质量

**理由**：
1. **语义浅层**：仅将 `is not` 改为 `is`，是最简单的布尔逻辑反转，没有体现对代码语义的深层理解。
2. **孤立性**：只修改一行，与周围代码逻辑孤立，不涉及任何调用关系或数据流传播。
3. **效果过于破坏性**：这个改动会让所有调用 `_calculate_separability_matrix()` 并返回有效矩阵的模型（如有自定义实现的模型）全部走错误路径，而让返回 `NotImplemented` 的模型（大多数模型）走正确路径。这会导致几乎所有测试失败，而不是精准地只让 F2P 测试失败。

**最终 mutation（A1 - 改变 API 参数语义）**：

在 `_cstack` 中，创建 `cright` 时错误地使用 `left.shape[1]`（left 的输入数）代替 `right.shape[1]`（right 的输入数）作为列数。

```diff
diff --git a/astropy/modeling/separable.py b/astropy/modeling/separable.py
index 45bea3608..a_new 100644
--- a/astropy/modeling/separable.py
+++ b/astropy/modeling/separable.py
@@ -242,7 +242,7 @@ def _cstack(left, right):
         cright = _coord_matrix(right, 'right', noutp)
     else:
-        cright = np.zeros((noutp, right.shape[1]))
+        cright = np.zeros((noutp, left.shape[1]))
         cright[-right.shape[0]:, -right.shape[1]:] = right
 
     return np.hstack([cleft, cright])
```

**变异语义**：开发者误以为并联操作（`&`）的两个子矩阵应该有相同的列数（输入维度），因为它们最终要 `hstack` 在一起。实际上 `cleft` 的列数是 `left` 的输入数，`cright` 的列数是 `right` 的输入数，它们通常不同。当 `left.shape[1] != right.shape[1]` 时（如 cm9/cm10/cm11 中 `(rot&sh1)(3输入)` 与 `sh2(1输入)` 的组合），`cright` 会有错误的列数，导致 `hstack` 后的总矩阵列数错误，separability 计算完全错误。对于 `left.shape[1] == right.shape[1]` 的情况（如 cm8 中两个 2×2 矩阵），行为不变，难以检测。

---

### Group B — 替换（低质量）

**原 mutation**：
```diff
-        cright[-right.shape[0]:, -right.shape[1]:] = right
+        cright[-right.shape[0]+1:, -right.shape[1]:] = right
```

**质量评估**：低质量

**理由**：
1. **语义浅层**：`+1` 是最典型的 off-by-one 变异，完全机械，没有语义理解。
2. **与 golden patch 同一行**：修改的是 golden patch 修复的同一行，虽然不是直接还原，但仍然是在同一个逻辑点上做最简单的修改。
3. **孤立性**：单行修改，影响范围局限。

**最终 mutation（B3 - 反转位置逻辑）**：

在 `_cstack` 中，将 `cleft` 的填充位置从顶部（`[:left.shape[0]]`）改为底部（`[noutp-left.shape[0]:]`），与 `cright` 的底部填充方式一致。

```diff
diff --git a/astropy/modeling/separable.py b/astropy/modeling/separable.py
index 45bea3608..b_new 100644
--- a/astropy/modeling/separable.py
+++ b/astropy/modeling/separable.py
@@ -238,7 +238,7 @@ def _cstack(left, right):
     else:
         cleft = np.zeros((noutp, left.shape[1]))
-        cleft[: left.shape[0], : left.shape[1]] = left
+        cleft[noutp - left.shape[0]:, : left.shape[1]] = left
     if isinstance(right, Model):
```

**变异语义**：`_cstack` 的设计约定是 `left` 子矩阵放在总矩阵的**顶部**（行 0 到 `left.n_outputs-1`），`right` 子矩阵放在**底部**（行 `-right.n_outputs` 到末尾）。这个 mutation 模拟了开发者误解这一约定，以为 `left` 也应该像 `right` 一样放在底部。结果是 `cleft` 和 `cright` 都被放在矩阵底部，发生重叠，顶部行全为零。这是一个**多层级**的 bug：它同时影响内层 `_cstack`（如 `sh1 & sh2`）和外层 `_cstack`（如 `rot & (sh1&sh2)`），造成级联错误。

---

### Group C — 替换（低质量）

**原 mutation**：
```diff
-        cright[-right.shape[0]:, -right.shape[1]:] = right
+        cright[-right.shape[0]:, -right.shape[1]:] = 1 if right.dtype == np.float64 else right
```

**质量评估**：低质量

**理由**：
1. **功能等价冗余**：numpy 矩阵默认使用 `float64`，所以条件 `right.dtype == np.float64` 几乎总为 True，使得这个 mutation 在所有实际测试场景下等同于直接还原 `= 1`（golden patch 修复之前的 bug）。
2. **不自然**：dtype 检查在这个上下文中毫无语义意义，是明显的人工痕迹。
3. **与 golden patch 同一行**：修改的是 golden patch 修复的同一行。

**最终 mutation（C1 - 混淆维度引用）**：

在 `_cstack` 中，`cright` 的行切片使用 `left.shape[0]`（left 的输出数）代替 `right.shape[0]`（right 的输出数）。

```diff
diff --git a/astropy/modeling/separable.py b/astropy/modeling/separable.py
index 45bea3608..c_new 100644
--- a/astropy/modeling/separable.py
+++ b/astropy/modeling/separable.py
@@ -242,7 +242,7 @@ def _cstack(left, right):
         cright = _coord_matrix(right, 'right', noutp)
     else:
         cright = np.zeros((noutp, right.shape[1]))
-        cright[-right.shape[0]:, -right.shape[1]:] = right
+        cright[-left.shape[0]:, -right.shape[1]:] = right
 
     return np.hstack([cleft, cright])
```

**变异语义**：开发者在确定 `cright` 的行切片范围时，错误地使用了 `left.shape[0]`（left 子模型的输出数）代替 `right.shape[0]`（right 子模型的输出数）。这模拟了一个真实的变量引用错误——在处理两个相关变量时混淆了它们的属性。当 `left.shape[0] != right.shape[0]`（如 cm9/cm10/cm11 中 `(rot&sh1)` 有3行而 `sh2` 只有1行），`cright` 的内容会被放在错误的行位置，导致 separability 矩阵的右下角块错位。

---

### Group D — 替换（低质量，完全冗余）

**原 mutation**：
```diff
-        cright[-right.shape[0]:, -right.shape[1]:] = right
+        cright[-right.shape[0]:, -right.shape[1]:] = 1
```

**质量评估**：低质量（完全冗余）

**理由**：
1. **完全冗余**：这就是 base_commit 中的原始代码（第245行原本就是 `= 1`）。这个 mutation 完全等同于还原 golden patch，没有引入任何新的 bug，只是撤销了修复。
2. **与 golden patch 同一行**：直接还原。

**最终 mutation（D3 - 引入顺序依赖错误）**：

在 `_separable` 中，交换 `sepleft` 和 `sepright` 的赋值（即用 `transform.right` 的结果赋给 `sepleft`，用 `transform.left` 的结果赋给 `sepright`）。

```diff
diff --git a/astropy/modeling/separable.py b/astropy/modeling/separable.py
index 45bea3608..d_new 100644
--- a/astropy/modeling/separable.py
+++ b/astropy/modeling/separable.py
@@ -304,8 +304,8 @@ def _separable(transform):
     if (transform_matrix := transform._calculate_separability_matrix()) is not NotImplemented:
         return transform_matrix
     elif isinstance(transform, CompoundModel):
-        sepleft = _separable(transform.left)
-        sepright = _separable(transform.right)
+        sepleft = _separable(transform.right)
+        sepright = _separable(transform.left)
         return _operators[transform.op](sepleft, sepright)
     elif isinstance(transform, Model):
         return _coord_matrix(transform, 'left', transform.n_outputs)
```

**变异语义**：这是一个**跨函数的语义 mutation**，修改了 `_separable` 对 CompoundModel 的递归处理逻辑。开发者在赋值时交换了 left 和 right 子树的结果，导致 `_cstack(sepleft, sepright)` 实际上接收到了颠倒的参数。对于**对称**的复合模型（如 `sh1 & sh2`，两者都是相同的1D Shift），交换后结果相同，测试通过；只有**非对称**的复合模型（如 `rot & (sh1&sh2)`，左子树是2D非可分模型，右子树是2D可分矩阵）才会暴露错误。这是一个典型的"对称性假设"错误——开发者可能认为 `&` 操作是可交换的。

---

### Group E — 替换（低质量，功能等价冗余）

**原 mutation**：
```diff
-        cright[-right.shape[0]:, -right.shape[1]:] = right
+        # Bug: Check if right is from a CompoundModel and use buggy logic
+        if isinstance(right, np.ndarray) and right.ndim == 2 and right.shape[0] > 1:
+            cright[-right.shape[0]:, -right.shape[1]:] = 1
+        else:
+            cright[-right.shape[0]:, -right.shape[1]:] = right
```

**质量评估**：低质量（功能等价冗余）

**理由**：
1. **功能等价冗余**：在 F2P 测试场景中，`right` 总是 2D ndarray 且 `right.shape[0] > 1`（如 `sh1 & sh2` 的结果是 2×2 矩阵），所以条件总为 True，等同于直接 `= 1`（golden patch 修复之前的 bug）。
2. **不自然**：代码注释直接写了 "Bug"，完全不符合真实代码风格。
3. **与 golden patch 同一行**：功能上还原了 golden patch 的修复。

**最终 mutation（E1 - 改变断言期望/判断条件）**：

在 `is_separable()` 中，将判断输出是否可分离的条件从 `row_sum != 1`（恰好依赖1个输入）改为 `row_sum < 1`（依赖少于1个输入）。

```diff
diff --git a/astropy/modeling/separable.py b/astropy/modeling/separable.py
index 45bea3608..e_new 100644
--- a/astropy/modeling/separable.py
+++ b/astropy/modeling/separable.py
@@ -60,7 +60,7 @@ def is_separable(transform):
     separable_matrix = _separable(transform)
     is_separable = separable_matrix.sum(1)
-    is_separable = np.where(is_separable != 1, False, True)
+    is_separable = np.where(is_separable < 1, False, True)
     return is_separable
```

**变异语义**：`is_separable` 的语义是"该输出恰好依赖且仅依赖1个输入"（行和 == 1）。`!= 1` 的条件正确排除了行和为0（无依赖）和行和 > 1（多依赖）的情况。改为 `< 1` 后，只有行和为0的输出才被标记为 False，而行和 > 1（依赖多个输入，如 Rotation2D 的输出）的输出会被错误地标记为 True（separable）。这模拟了开发者对 "separable" 定义的误解：将其理解为"至少有一个输入依赖"而不是"恰好有一个输入依赖"。对于 cm8-cm11，`rot` 的两个输出各依赖2个输入（行和=2），`< 1` 条件为 False，所以 `where(False, False, True) = True`，错误地标记为 separable。

---

## 新设计 Mutation 说明

### 设计方法论

通过深度阅读仓库代码，发现了以下关键事实：

1. **死代码发现**：`_cstack` 中的 `isinstance(left/right, Model)` 分支在正常的 `_separable` 调用链中**永远不会执行**，因为 `_separable` 总是返回 ndarray。真正的执行路径只走 `else`（ndarray）分支。

2. **矩阵都是 0/1 二值**：在 `&` 操作链中，所有中间矩阵都是 0/1 二值矩阵。这意味着基于值的 mutation（如 clip、normalize）对 `&` 操作无效。只有基于**结构**（位置、形状、顺序）的 mutation 才有效。

3. **F2P 测试的特征**：新增的 F2P 测试（cm8-cm11）都只用了 `&` 操作符，不用 `|`。因此，只有影响 `_cstack` 或 `_separable` CompoundModel 分支的 mutation 才能使 F2P 测试失败。

4. **多层级效应**：`_cstack` 的 bug 会在递归调用中放大——内层 `_cstack` 的错误结果作为外层 `_cstack` 的输入，导致级联错误（如 B mutation 同时影响 `sh1 & sh2` 和 `rot & (sh1&sh2)` 两层）。

### 各 Mutation 的代码依据

- **A mutation**：基于对 `_cstack` 函数签名的分析——`left` 和 `right` 分别代表左右子模型的坐标矩阵，它们的列数（输入维度）通常不同（`left.shape[1]` vs `right.shape[1]`）。混淆这两个维度是一个自然的开发者错误。

- **B mutation**：基于对 `_cstack` 矩阵布局约定的分析——左子矩阵放顶部（`[:n]`），右子矩阵放底部（`[-n:]`）。将两者都放底部会导致重叠，是一个真实的对称性误解。

- **C mutation**：基于对 `_cstack` 中行/列切片的分析——`cright[-right.shape[0]:, -right.shape[1]:]` 中行切片用的是 `right.shape[0]`，列切片用的是 `right.shape[1]`。用 `left.shape[0]` 替换 `right.shape[0]` 是一个自然的变量混淆错误，在 `left.shape[0] != right.shape[0]` 时才暴露。

- **D mutation**：基于对 `_separable` CompoundModel 分支的分析——递归计算 `sepleft = _separable(transform.left)` 和 `sepright = _separable(transform.right)` 后传给 `_operators[op]`。交换赋值顺序是一个跨函数的语义错误，对对称复合模型无效，只在非对称嵌套时暴露。

- **E mutation**：基于对 `is_separable` 判断逻辑的分析——`row_sum != 1` 正确地将 separable 定义为"恰好依赖1个输入"。将其改为 `row_sum < 1` 改变了 separable 的语义定义，是一个对 API 语义的误解。
