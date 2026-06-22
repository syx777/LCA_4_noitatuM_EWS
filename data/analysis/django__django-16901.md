# django__django-16901

## 问题背景

在不原生支持 XOR 的数据库（如 PostgreSQL）上，`Q(…) ^ Q(…) ^ Q(…)`（超过 2 个参数）被错误地解释为"恰好一个为真"而非"奇数个为真"（parity）。原生支持 XOR 的数据库（如 MySQL）以及 XOR 的正确语义是：`a ^ b ^ c` 在奇数个参数为真时成立。Django 的 fallback SQL 却生成 `(a OR b OR ...) AND (a+b+...) == 1`（恰好一个）。Golden patch 引入 `Mod`：当子节点数 > 2 时，把 `rhs_sum` 取模 2，即 `MOD(a+b+..., 2) == 1`（奇数个）。

## Golden Patch 语义分析

```python
lhs = self.__class__(self.children, OR)
rhs_sum = reduce(operator.add, (Case(When(c, then=1), default=0) for c in self.children))
if len(self.children) > 2:
    rhs_sum = Mod(rhs_sum, 2)
rhs = Exact(1, rhs_sum)
return self.__class__([lhs, rhs], AND, self.negated).as_sql(...)
```
核心语义：**n 元 XOR 的 fallback 应为"OR 且 真值个数为奇数"。当 `len(children) > 2` 时把真值之和 `rhs_sum` 取 `Mod(rhs_sum, 2)`，再与 1 比较（`Exact(1, ...)`）**。关键点：阈值 `> 2`（2 元 XOR 等价 exactly-one，无需取模）、模数 `2`（奇偶）、`Mod` 参数顺序（被除数在前）、比较目标 `Exact(1, ...)`（奇数=1）。

F2P 测试 `XorLookupsTests.test_filter_multiple`：5 个 `Q(num__gte=...)` 异或，断言结果与 Python `(i>=1)^(i>=3)^...` 的 parity 一致。

## 调用链分析

`WhereNode.as_sql` 中，当 `connector == XOR and not connection.features.supports_logical_xor`（sqlite/postgres）走 fallback：构造 OR 子句 + 真值和；`len > 2` 时 `Mod(rhs_sum, 2)`；`Exact(1, rhs_sum)` 判定。阈值、模数、Mod 参数序、Exact 目标值任一出错，parity 语义即被破坏（退回 exactly-one 或取反）。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 新增 | 新增 | `Mod(rhs_sum, 2)`→`Mod(2, rhs_sum)`，被除数/除数对调 |
| B | 🟢 高质量 | 保留 | 阈值 `> 2`→`<= 2`，>2 时不取模 |
| C | 🟢 高质量 | 保留 | 模数 `2`→`3`，按 mod 3 判奇偶 |
| D | ➕ 新增 | 新增 | `Exact(1, ...)`→`Exact(0, ...)`，parity 取反 |
| E | 🟢 高质量 | 保留 | 取模藏到 `parity_xor` 开关后 |

原始 B/C/E 都改阈值 `len(self.children) > 2`（B=`<2`、C=`==2`、E=开关），机制趋同。保留 B（改 `<=2`，与原 C/E 区分）、C（改模数）、E（开关），补充 A（Mod 参数对调）、D（Exact 目标值）。

## 各组 Mutation 分析

### Group A — 新增（A1 接口契约：Mod 参数对调）
```diff
-                rhs_sum = Mod(rhs_sum, 2)
+                rhs_sum = Mod(2, rhs_sum)
```
**变异语义**：`Mod(rhs_sum, 2)`（sum % 2）两参数对调成 `Mod(2, rhs_sum)`（2 % sum）。计算的是 `2 mod 真值和` 而非 `真值和 mod 2`，奇偶判定完全错——如 sum=3 时 `2%3=2≠1`、sum=1 时 `2%1=0≠1`。F2P 的 5 元异或 parity 结果全错。模拟"取模两操作数写反"。新增为 A。

### Group B — 保留（B3 阈值反转）
```diff
-            if len(self.children) > 2:
+            if len(self.children) <= 2:
```
**变异语义**：取模阈值 `> 2` 反转为 `<= 2`。只有 2 个及以下子节点才取模（而 2 元 XOR 本就等价 exactly-one、取模无害也无意义），>2 个时**不**取模 → 退回 `Exact(1, sum)` 的 exactly-one 错误语义。F2P 的 5 元（>2）异或不取模 → 失败。保留。

### Group C — 保留（C1 值：模数错）
```diff
-                rhs_sum = Mod(rhs_sum, 2)
+                rhs_sum = Mod(rhs_sum, 3)
```
**变异语义**：模数 2 写成 3，`Mod(rhs_sum, 3)`——按 mod 3 而非 mod 2 判定。`sum % 3 == 1` 与 `sum % 2 == 1`（奇数）不同：如 sum=3 时 `3%3=0≠1`（漏）、sum=2 时 `2%3=2≠1`。parity 结果错。模拟"模数常量写错"。F2P 失败。保留。

### Group D — 新增（C1 值：比较目标取反）
```diff
-            rhs = Exact(1, rhs_sum)
+            rhs = Exact(0, rhs_sum)
```
**变异语义**：比较目标 `Exact(1, rhs_sum)` 改成 `Exact(0, rhs_sum)`——判定 `sum % 2 == 0`（偶数个为真）而非 `== 1`（奇数个）。XOR parity 整体取反，所有结果颠倒。注意此处 `Exact(value, expr)` 的第一个参数是比较值。模拟"判定目标值 1 写成 0、把奇偶判反"。F2P 失败。新增为 D（作用于 Exact 而非阈值/Mod，与 A/B/C 区分）。

### Group E — 保留（E2 隐式→显式开关）
```diff
-            if len(self.children) > 2:
+            if len(self.children) > 2 and getattr(self, "parity_xor", False):
```
**变异语义**：取模条件追加 `and getattr(self, "parity_xor", False)` 开关（默认不存在 → False）。默认即使 >2 个子节点也不取模 → 退回原 bug 的 exactly-one 语义。只有显式设 `parity_xor=True` 才走 parity。模拟"把 parity 取模做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

原始 B/C/E 都改阈值 `len(self.children) > 2`（B 改 `<2`、C 改 `==2`、E 加开关），机制高度趋同，且都只动阈值这一处。本次保留 B（阈值反转 `<=2`，覆盖 >2 不取模）、C（模数 2→3）、E（parity_xor 默认关闭开关），补充 A（Mod 参数对调）、D（Exact 目标 1→0 取反 parity）。五组覆盖"Mod 参数序 / 阈值反转 / 模数错 / 比较目标取反 / 默认关闭开关"五个角度，分别作用于 Mod 调用、阈值、模数常量、Exact 比较值、特性开关五个环节——全部破坏 n 元 XOR 的 parity 语义。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
