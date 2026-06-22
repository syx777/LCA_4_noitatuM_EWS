# matplotlib__matplotlib-24970

## 问题背景

NumPy 1.24 起，`Colormap.__call__` 处理整型（如 uint8）输入时触发 deprecation 警告/错误：把越界哨兵（`_i_over`=N、`_i_under`=N+1 等）赋给 uint8 数组会溢出。根因：原代码把 `xa = xa.astype(int)` 放在 `if xa.dtype.kind == "f"` 浮点分支内，整型输入（uint8）跳过该分支、保持 uint8 dtype，后续 `xa[xa > N-1] = _i_over` 用越界值赋 uint8 溢出。Golden patch 把 int cast 移出浮点分支、放到无条件的 `with np.errstate` 块——整型也被统一 cast 成 int。

## Golden Patch 语义分析

```python
if xa.dtype.kind == "f":
    xa *= self.N
    xa[xa < 0] = -1
    xa[xa == self.N] = self.N - 1
    np.clip(xa, -1, self.N, out=xa)
with np.errstate(invalid="ignore"):
    # We need this cast for unsigned ints as well as floats
    xa = xa.astype(int)
```
核心语义：**`xa.astype(int)` 必须无条件执行（移出浮点分支），使整型（uint8 等）输入也被 cast 成有符号 int，后续越界哨兵赋值才不溢出**。关键点：cast 在浮点分支**之外**、cast 目标是 `int`（有符号、足够宽）、对所有 dtype 生效。

F2P 测试 `test_colors.py::test_index_dtype`（参数化 uint8/int/float16/float）：`cm(dtype(0)) == cm(0)`——各 dtype 结果一致。

## 调用链分析

`cm(np.uint8(0))` → `Colormap.__call__` → 整型跳过浮点分支 → `with np.errstate: xa = xa.astype(int)`（golden 统一 cast）→ `xa[xa>N-1]=_i_over` 等哨兵赋值（int 不溢出）→ 索引 lut。cast 缺失/在浮点分支内/目标类型错/dtype 判定反转/门控开关，都会让 uint8 path 不被 cast 成 int、哨兵溢出、结果与 int path 不一致。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 新增 | 重做 | 删除无条件 int cast，整型未 cast |
| B | 🟢 高质量 | 保留 | `dtype.kind == "f"`→`!= "f"`，分支反转 |
| C | ➕ 新增 | 重做 | cast 目标 `int`→`float`，索引非整型 |
| D | 🟢 高质量 | 重做 | cast `int`→`xa.dtype`，保持 uint8 |
| E | ➕ 新增 | 重做 | int cast 藏到 _CAST_ALL_INT 开关后 |

原始仅 B、D（D 与 A 注释/删 init 等重复）。numpy 1.21（无 1.24 deprecation-as-error）下，须用真正改变结果的机制。保留 B，重做 A（删 cast）、C（cast float）、D（cast 保持 dtype）、E（开关）。

## 各组 Mutation 分析

### Group A — 重做（D1 状态：删 int cast）
```diff
-        with np.errstate(invalid="ignore"):
-            # We need this cast for unsigned ints as well as floats
-            xa = xa.astype(int)
```
**变异语义**：删除无条件的 `xa = xa.astype(int)` 块——uint8 等整型 X 不再被 cast 成有符号 int。后续 `xa[xa < 0] = self._i_under`（-1）对 uint8 数组回绕成 255、`_i_over`=N=256 回绕成 0，索引到错误的 lut 项，`cm(uint8(0)) != cm(0)`。`test_index_dtype` 失败。重做为 A。

### Group B — 保留（B3 条件反转：dtype 判定）
```diff
-        if xa.dtype.kind == "f":
+        if xa.dtype.kind != "f":
```
**变异语义**：`xa.dtype.kind == "f"` 反转成 `!= "f"`——浮点 X 不走缩放/裁剪分支（`*= N` 等），整型 X 反而走（整型乘 N、clip 等错误操作）。dtype 处理颠倒，`test_index_dtype`（float vs int 一致）失败。保留。

### Group C — 重做（C1 类型：cast float）
**原**：与删 init 等重复。
**最终 mutation**：
```diff
-            xa = xa.astype(int)
+            xa = xa.astype(float)
```
**变异语义**：cast 目标 `int` 改成 `float`——xa 保持浮点。后续 `xa[xa > self.N - 1] = self._i_over` 等用作 lut 数组索引时，浮点数组不能作整型索引（IndexError）或被隐式处理出错。cast 目标类型错。F2P 失败。重做为 C。

### Group D — 重做（C1 值：cast 保持 dtype）
**原**：与 B 等重复机制。
**最终 mutation**：
```diff
-            xa = xa.astype(int)
+            xa = xa.astype(xa.dtype)
```
**变异语义**：cast `int` 改成 `xa.astype(xa.dtype)`（cast 成自身 dtype，等于没转）——uint8 输入仍是 uint8，`_i_under`=-1 对 uint8 回绕成 255，越界索引错。形同 golden 修复前（整型未统一成 int）。比 A（删 cast）保留了 cast 语句、只是目标无效。F2P 失败。重做为 D。

### Group E — 重做（E2 隐式→显式开关）
```diff
-        with np.errstate(invalid="ignore"):
-            # We need this cast for unsigned ints as well as floats
-            xa = xa.astype(int)
+        if globals().get("_CAST_ALL_INT", False):
+            with np.errstate(invalid="ignore"):
+                xa = xa.astype(int)
```
**变异语义**：无条件 int cast 藏到模块级 `_CAST_ALL_INT`（默认 False）开关后——默认不对整型统一 cast int（uint8 path 保持 uint8），还原整型回绕 bug。`Colormap.__call__` 是方法但此处用 `globals()` 模块级开关（避免改签名）。只有设 True 才 cast。默认即 bug。F2P 失败。重做为 E。

## 新设计 Mutation 说明

原始仅 B、D 两组，且 D 与"删 init/注释"等机制重复；且本实例运行在 numpy 1.21（无 1.24 的 deprecation-as-error），故基于"越界值溢出报错"的变异不会触发——必须用真正改变索引结果的机制。本次保留 B（dtype 判定反转），重做 A（删无条件 int cast、uint8 回绕）、C（cast 成 float、索引非整型）、D（cast 成自身 dtype、保持 uint8 回绕）、E（`_CAST_ALL_INT` 默认关闭开关）。五组覆盖"删 cast / dtype 判定反转 / cast float / cast 保持 dtype / 默认关闭开关"五个角度——全部令整型输入不被统一成有符号 int、索引结果与 int path 不一致。全部实测（Python 3.9/matplotlib 3.6.0 + numpy 1.21.6，源码构建 C 扩展，conda 编译器）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
