# django__django-14017

## 问题背景

`Exists(...) & Q()` 可以工作，但反过来 `Q() & Exists(...)` 抛 `TypeError`。`&`/`|` 在 `Q` 与 `Exists` 之间本应是可交换的，但不是。根因：`Q._combine`（`query_utils.py`）在合并前用 `if not isinstance(other, Q): raise TypeError(other)` 校验对方类型，只接受 `Q` 实例。当左操作数是 `Q`、右操作数是 `Exists` 时，`Q.__and__/__or__` 调用 `self._combine(Exists对象)`，因 `Exists` 不是 `Q` 而报错。

Golden patch 放宽该校验：除 `Q` 外，也接受任何"条件表达式"——即带有 `conditional` 属性且为真的对象（`Exists` 的 `conditional` 属性为 `True`，因其 `output_field` 是 `BooleanField`）。

## Golden Patch 语义分析

```python
def _combine(self, other, conn):
    if not(isinstance(other, Q) or getattr(other, 'conditional', False) is True):
        raise TypeError(other)
    ...
```

核心语义：把"可与 `Q` 合并的右操作数"从"仅 `Q` 实例"扩展为"`Q` 实例 **或** 一个条件表达式（`conditional is True`）"。

- `getattr(other, 'conditional', False)`：安全读取 `other.conditional`，缺失时默认 `False`。
- `is True`：严格身份比较，要求该属性**恰为布尔 `True`**（而非任意真值）。`Q.conditional = True`（类属性）、`Exists.conditional` 是一个返回 `isinstance(output_field, BooleanField)` 的 property，对 `Exists` 求值为 `True`。
- 两个条件用 `or` 连接，任一为真即放行；都为假才 `raise TypeError`。

放行后，`_combine` 继续：若 `other` 为空（`not other`）则复制 `self`；若 `self` 为空则复制 `other`；否则建一个新 `Q`，把 `self`、`other` 作为子节点 `add` 进去，由查询编译期再处理 `Exists` 子节点。

F2P 测试两个：
- `test_boolean_expression_combined`：新增 `Q(salary__gte=30) & Exists(is_ceo)`、`Q(salary__lt=15) | Exists(is_poc)` 等断言（Q 在左、Exists 在右）。
- `test_boolean_expression_combined_with_empty_Q`：遍历 `Exists & Q()`、`Q() & Exists`、`Exists | Q()`、`Q() | Exists` 四种组合，断言过滤结果一致。
任何让"右操作数为 `Exists` 时不被放行"或"放行逻辑被改坏"的变异都会让这些组合重新抛 `TypeError`（测试 ERROR）。

## 调用链分析

- 入口：`Q.__and__(self, other)` / `Q.__or__` → `self._combine(other, conn)`（query_utils.py:61-65）。当用户写 `Q() & Exists(...)`，Python 调用左操作数 `Q` 的 `__and__`，`other` 即 `Exists` 对象。
- 反方向 `Exists(...) & Q()` 走的是 `expressions.Combinable.__and__`（expressions.py:92），其逻辑 `if getattr(self,'conditional',False) and getattr(other,'conditional',False): return Q(self) & other`，本就能处理——这解释了"一个方向行、另一个方向不行"的不对称。Golden patch 修的是 `Q` 侧。
- 关键属性来源：
  - `Q.conditional`：类属性，硬编码 `= True`（query_utils.py:37）。
  - `Exists.conditional`：继承自 `BaseExpression` 的 **property**（expressions.py:255-257），返回 `isinstance(self.output_field, fields.BooleanField)`；`Exists.output_field = BooleanField()`，故为 `True`。
- 数据流区分：校验通过后，对 `Q() & Exists`（self 为空 Q）走 `elif not self: return type(other)(...other.deconstruct()...)` 之外的路径——实际 `Exists` 无 `deconstruct` 兼容的此分支语义，最终落到 `obj = type(self)(); obj.add(self); obj.add(other)` 建复合 `Q`。F2P 的"空 Q"组合尤其依赖：空 Q 被忽略、留下 `Exists` 单独成为过滤条件。
- 因此变异的攻击面集中在那一行**类型放行条件**（A/B/C/D/E 多数），以及"空 Q 忽略"分支（原 B 曾尝试，但会误伤 P2P）。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟡 语义浅层（保留） | 保留 | 原 mutation 把 `is True` 改 `is False`，单符号替换；位于类型放行这一关键节点，能模拟"写反身份比较常量"的真实失误，且实测只命中 2 个 F2P、不波及 P2P，是高价值的边界判断变异 |
| B | 🔴 必须替换 | 替换 | 原 mutation 把 `not other` 改 `other`（空 Q 忽略分支取反），实测**误伤 P2P** `test_subquery`，不满足"只让 F2P 失败"，无效 |
| C | 🔴 必须替换 | 替换 | 原 mutation 字节级还原为 `not isinstance(other, Q)`，是 golden patch 逆操作，直接冗余 |
| D | 🔴 必须替换 | 替换 | 同 C，字节级还原，直接冗余 |
| E | 🔴 必须替换 | 替换 | 同 C，字节级还原，直接冗余 |

语义浅层共 1 个（A）；按规则替换其中最弱的 floor(1/2)=**0** 个，故 A 保留。必须替换 4 个（B/C/D/E）：B 因误伤 P2P 无效，C/D/E 为三份相同的 golden 逆操作。全部替换为高质量变异。

## 各组 Mutation 分析

五个变异多数聚焦于 `_combine` 的类型放行条件，但分布在互不重叠的语义维度：**身份常量取反（A，保留）、布尔连接符 or→and（B）、类型/数值身份强转（C）、属性来源拼写（D）、显式开关门控（E）**。共同效果：右操作数为 `Exists` 时不再被放行（或放行被改坏），`Q() & Exists` 等组合重新抛 `TypeError`，2 个 F2P 测试 ERROR；而其余 58 个不涉及 Q-条件表达式合并的测试全部通过。

### Group A — 保留（🟡 语义浅层）
**原 mutation**：
```diff
-        if not(isinstance(other, Q) or getattr(other, 'conditional', False) is True):
+        if not(isinstance(other, Q) or getattr(other, 'conditional', False) is False):
```
**分类**：🟡 语义浅层（保留）
**理由**：单符号替换 `is True` → `is False`。虽是一行单点修改，但落在 golden patch 的核心放行条件上，模拟开发者"把身份比较常量写反"的真实失误。语义上变成"当 `other.conditional is False` 时也放行"——对 `Exists`（`conditional` 为 `True`）两个分支都不满足（既非 `Q`、`conditional is False` 也为假），于是被 `raise TypeError`，F2P 失败。实测只命中 2 个 F2P、不波及任何 P2P，是高质量的边界判断变异，按"语义浅层替换最弱 floor(1/2)=0 个"规则予以保留。
**最终 mutation**（与原相同）：
```diff
-        if not(isinstance(other, Q) or getattr(other, 'conditional', False) is True):
+        if not(isinstance(other, Q) or getattr(other, 'conditional', False) is False):
```
**变异语义**：把"接受 conditional 为 True 的对象"偷换成"接受 conditional 为 False 的对象"。`Exists.conditional` 为 `True`，`True is False` 为假，且它不是 `Q`，故落入 `raise TypeError`。审查者容易把 `is False` 看成与 `is True` 等效的"判断 conditional 标志"，忽略身份常量被反转。F2P 失败，P2P 全过。

### Group B — 替换（🔴 原误伤 P2P）
**原 mutation**：
```diff
         # If the other Q() is empty, ignore it and just use `self`.
-        if not other:
+        if other:
```
**分类**：🔴 必须替换
**理由**：把"空 Q 才忽略"分支的条件取反，实测不仅让 F2P 失败，还**误伤 P2P** `test_subquery`（破坏了非空 Q 合并的正常路径），违反"只让 F2P 失败、保持 P2P 通过"的硬约束，无效。

**最终 mutation**：
```diff
-        if not(isinstance(other, Q) or getattr(other, 'conditional', False) is True):
+        if not(isinstance(other, Q) and getattr(other, 'conditional', False) is True):
```
**变异语义**：把放行条件里的 `or` 改成 `and`。原意"是 Q **或** 是条件表达式即放行"，变成"既是 Q **又** conditional is True 才放行"。对 `Exists`：`isinstance(other, Q)` 为假，`False and ...` 整体为假，`not(False)` 为真 → `raise TypeError`，`Q() & Exists` 失败。对普通 `Q`：`isinstance` 为真但 `Q().conditional is True` 也为真，二者 `and` 仍真——所以 **Q 与 Q 的合并不受影响**，P2P 全过。这是极隐蔽的逻辑连接符混淆：`and`/`or` 一字之差，且对最常见的 Q-Q 合并行为完全正常，只有 Q-Exists 这种混合合并才暴露。属 B3（布尔逻辑/连接符反转）。

### Group C — 替换（🔴 原 golden 逆操作）
**原 mutation**：
```diff
-        if not(isinstance(other, Q) or getattr(other, 'conditional', False) is True):
+        if not isinstance(other, Q):
```
**分类**：🔴 必须替换（字节级还原 golden patch，直接冗余）

**最终 mutation**：
```diff
-        if not(isinstance(other, Q) or getattr(other, 'conditional', False) is True):
+        if not(isinstance(other, Q) or int(getattr(other, 'conditional', False)) is True):
```
**变异语义**：在 `conditional` 值外套一层 `int(...)`，再 `is True`。表面像是"把 conditional 规范化成整数布尔再比较"的防御性写法。但 `int(True)` 得到的是整数 `1`，而 `1 is True` 为 `False`（`1` 与单例 `True` 不是同一对象）——身份比较因类型被强转为 `int` 而**永远不成立**。于是对 `Exists`：`int(True) is True` → `1 is True` → `False`，且非 `Q`，落入 `raise TypeError`，F2P 失败。对 Q-Q 合并不走这条 `or` 右支（`isinstance` 已为真），P2P 全过。`int()` 包裹看似无害的归一化，实则破坏了 `is` 身份比较所依赖的"对象必须是布尔单例"前提。属 C1（类型/数值身份强转破坏隐式约定：bool→int 后 `is True` 失效）。

### Group D — 替换（🔴 原 golden 逆操作）
**原 mutation**：
```diff
-        if not(isinstance(other, Q) or getattr(other, 'conditional', False) is True):
+        if not isinstance(other, Q):
```
**分类**：🔴 必须替换（字节级还原 golden patch，直接冗余）

**最终 mutation**：
```diff
-        if not(isinstance(other, Q) or getattr(other, 'conditional', False) is True):
+        if not(isinstance(other, Q) or getattr(other, '_conditional', False) is True):
```
**变异语义**：把读取的属性名从 `'conditional'` 改成 `'_conditional'`（多一个下划线，伪装成"内部/私有属性"的等价访问）。但 `Exists`/`Q` 上根本没有 `_conditional` 属性，`getattr(other, '_conditional', False)` 恒返回默认 `False`，`False is True` 为假；右操作数为 `Exists` 时整条放行条件不成立，`raise TypeError`，F2P 失败。对 Q-Q 合并由 `isinstance` 短路放行，P2P 全过。审查者看到 `getattr(other, '_conditional', ...)` 容易以为是访问某个私有标志、与公共 `conditional` 等价，却忽略了该属性名在代码库中并不存在、`getattr` 默认值悄悄吞掉了差异。属 D1（状态/属性来源错误：读取了一个未定义、未初始化为目标语义的属性名）。

### Group E — 替换（🔴 原 golden 逆操作）
**原 mutation**：
```diff
-        if not(isinstance(other, Q) or getattr(other, 'conditional', False) is True):
+        if not isinstance(other, Q):
```
**分类**：🔴 必须替换（字节级还原 golden patch，直接冗余）

**最终 mutation**：
```diff
     default = AND
     conditional = True
+    combine_conditional = False
...
-        if not(isinstance(other, Q) or getattr(other, 'conditional', False) is True):
+        if not(isinstance(other, Q) or (self.combine_conditional and getattr(other, 'conditional', False) is True)):
```
**变异语义**：新增一个默认 `False` 的类属性 `combine_conditional`，并把"接受条件表达式"这一放行项门控在它之上。看起来像是一次合理的"提供开关控制是否允许 Q 与条件表达式合并"的特性增强，类属性 + `and` 门控都符合常见可配置代码风格。但默认 `False` 意味着 `self.combine_conditional and ...` 恒为假，条件表达式放行项被静默关闭——等价于回到只接受 `Q` 的旧行为。`Q() & Exists` 重新抛 `TypeError`，F2P 失败。对 Q-Q 合并由前半 `isinstance` 短路放行，P2P 全过。审查者只会觉得多了个无害的特性旗标，而不会意识到**默认值把 golden 修复关掉了**。属 E2（隐式行为被改为显式开关门控，默认值使其失效）。

## 新设计 Mutation 说明

A 组保留原 mutation（高质量语义浅层，落在关键放行节点、只命中 F2P）。B/C/D/E 四组替换为全新设计，分别攻击四个互不重叠的语义维度，且都规避了原 mutation 的问题（B 不再误伤 P2P；C/D/E 不再是 golden 逆操作）：

- **B（B3）**：`or → and`，利用 Q-Q 合并下 `and` 两边恒真、行为不变的特性，只让 Q-Exists 混合合并暴露。
- **C（C1）**：`int()` 包裹后 `is True`，利用 `int(True)==1` 而 `1 is not True` 的身份比较陷阱，伪装成布尔归一化。
- **D（D1）**：`conditional → _conditional`，利用 `getattr` 默认值吞掉不存在属性的差异，伪装成私有属性访问。
- **E（E2）**：新增默认 `False` 的 `combine_conditional` 开关门控放行项，伪装成可配置增强。

全部仅修改 `django/db/models/query_utils.py`（允许文件），不触碰测试文件。均通过 Step 5 实证自查：在 base_commit → golden patch → test_patch 之后用 `git diff HEAD` 生成、`py_compile` 通过（C 组特别确认无 `is` 字面量比较的 SyntaxWarning），并实际运行整个 `expressions.tests.BasicExpressionsTests`（60 个测试）确认每个变异都**只**使 2 个 F2P 测试 `test_boolean_expression_combined` 与 `test_boolean_expression_combined_with_empty_Q` 失败（均为 `TypeError` 引发的 ERROR），其余 58 个测试全部通过（无附带破坏，尤其不再误伤原 B 命中的 `test_subquery`）。
