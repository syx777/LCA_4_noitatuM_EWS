# django__django-14007

## 问题背景

当用户自定义一个 `BigAutoField` 子类并实现 `from_db_value`（例如把数据库返回的整数包装成 `MyIntWrapper`）时，普通查询路径会正确调用该转换器，但**插入（INSERT）时返回的主键值**却绕过了所有数据库转换器（`from_db_value`），直接把裸整数赋给了实例属性。

```python
class MyAutoField(models.BigAutoField):
    def from_db_value(self, value, expression, connection):
        return MyIntWrapper(value)

>>> AutoModel.objects.first().id      # <MyIntWrapper: 1>  ✅ 查询走转换器
>>> AutoModel.objects.create().id     # 2                  ❌ 插入返回值未走转换器
```

该缺陷同样影响 `bulk_create`（在支持取回主键的后端上）。Golden patch 在 `SQLInsertCompiler.execute_sql` 中，把三条返回分支的结果统一收集到 `rows`，然后**对 `returning_fields` 构造列表达式、取转换器并应用**，最后才返回，从而让插入返回值也经过 `from_db_value`。

## Golden Patch 语义分析

```python
def execute_sql(self, returning_fields=None):
    ...
    opts = self.query.get_meta()
    self.returning_fields = returning_fields
    with self.connection.cursor() as cursor:
        ...
        if ... can_return_rows_from_bulk_insert and len(objs) > 1:
            rows = self.connection.ops.fetch_returned_insert_rows(cursor)
        elif ... can_return_columns_from_insert:
            rows = [self.connection.ops.fetch_returned_insert_columns(cursor, self.returning_params)]
        else:
            rows = [(self.connection.ops.last_insert_id(cursor, opts.db_table, opts.pk.column),)]
    cols = [field.get_col(opts.db_table) for field in self.returning_fields]
    converters = self.get_converters(cols)
    if converters:
        rows = list(self.apply_converters(rows, converters))
    return rows
```

核心语义有三层，缺一不可：

1. **统一返回路径**：原来三条分支各自 `return`，patch 改成把结果存入 `rows`、跳出 `with` 块后统一处理。这要求 `self.returning_fields` 必须先被赋值（`get_col`/转换器都依赖它）。
2. **构造列表达式并取转换器**：`cols = [field.get_col(opts.db_table) for field in self.returning_fields]`，再 `get_converters(cols)`。`get_converters` 会探测每个表达式是否有 `from_db_value`（`Field.get_db_converters` 检查 `hasattr(self, 'from_db_value')`）。
3. **应用转换器并物化**：`rows = list(self.apply_converters(rows, converters))`。`apply_converters(self, rows, converters)` 是**生成器**，必须用 `list()` 物化；且参数顺序固定为 `(rows, converters)`。返回值 `rows` 必须是**可下标/可 zip 的列表**，因为调用方 `Model._save_table` 会执行 `for value, field in zip(results[0], returning_fields)`。

F2P 测试 `test_auto_field_subclass_create` 断言 `CustomAutoFieldModel.objects.create().id` 是 `MyWrapper` 实例。只要转换器没被取到、没被应用、或返回值形态被破坏，断言即失败。`test_auto_field_subclass_bulk_create` 需 `can_return_rows_from_bulk_insert`，在 SQLite 上被 `@skipUnlessDBFeature` 跳过。

## 调用链分析

- 调用链：`Model.save` → `Model._save_table`（base.py:872 `_do_insert`）→ `QuerySet._insert`（query.py:1289）→ `query.get_compiler(using).execute_sql(returning_fields)`。
- `execute_sql` 返回 `rows` 后，`_save_table` 在 base.py:873-875 执行 `for value, field in zip(results[0], returning_fields): setattr(self, field.attname, value)`——**因此返回值必须可下标 `results[0]` 且可 `zip`**，这是 C 组变异的攻击面。
- SQLite 后端：`can_return_rows_from_bulk_insert` 与 `can_return_columns_from_insert` 均为 `False`，所以单条 `create()` 走 **else 分支**（`last_insert_id`），`rows = [(pk,)]`（单行单列）。这意味着：
  - `len(rows) == 1`（B 组利用：`len(rows) > 1` 条件下单行 create 被排除）。
  - `can_return_columns_from_insert == False`（D 组利用：用它作门控会恒假，跳过转换）。
- `get_converters(cols)`（compiler.py:1100）逐个表达式调用 `get_db_converters`；`MyAutoField` 定义了 `from_db_value`，故 `converters` 非空。
- `apply_converters(self, rows, converters)`（compiler.py:1110）是生成器：`converters = list(converters.items())`，逐行按 `pos` 取值并依次套用转换器。**参数顺序敏感**（A 组利用：传反会把 dict 当 rows 遍历、把 list 当 converters，立即出错）；**返回生成器**（C 组利用：不 `list()` 物化则 `return` 出一个 generator，下游 `results[0]` 失败）。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原 mutation `get_converters([])` 传空列表，converters 恒空 → 功能等价于还原 golden（转换永不应用），冗余 |
| B | 🔴 必须替换 | 替换 | 原 mutation `if not converters:` 把条件取反，逻辑明显错乱（无转换器才应用），且功能等价于"永不正确应用"，不自然 |
| C | 🔴 必须替换 | 替换 | 原 mutation 把 `if/apply` 两行用 `#` 注释掉 → golden 的字节级逆操作，且注释痕迹明显 |
| D | 🔴 必须替换 | 替换 | 原 mutation 把 `self.returning_fields = returning_fields` 用 `#` 注释掉 → 破坏 golden 的前置赋值，注释痕迹明显且会牵连其它分支 |
| E | 🟢 保留 | 保留 | 新增 `apply_converters_on_insert=False` 参数门控，默认关闭即静默失效，属合理"opt-in 开关"接口变更，隐蔽 |

语义浅层共 0 个；必须替换 4 个（A/B/C/D），全部替换为高质量变异；E 组保留。

## 各组 Mutation 分析

所有最终变异都落在 golden patch 新增的"取转换器 → 应用转换器"这一段，但分布在五个互不重叠的语义维度：**调用参数契约（A）、边界条件（B）、返回值形态（C）、后端特性依赖（D）、显式参数门控（E）**。共同效果：插入返回的主键不再经过 `from_db_value` → `id` 仍是裸 int，F2P 失败；而 14 个不涉及自定义转换器的 `custom_pk` 测试全部通过。

### Group A — 替换
**原 mutation**：
```diff
-        converters = self.get_converters(cols)
+        converters = self.get_converters([])
```
**分类**：🔴 必须替换（功能等价冗余）
**理由**：传入空列表 `[]`，`get_converters` 遍历空序列直接返回 `{}`，`if converters:` 恒假，转换器永不应用——这与 golden patch 从未存在等价，是功能等价的逆操作。

**最终 mutation**：
```diff
         if converters:
-            rows = list(self.apply_converters(rows, converters))
+            rows = list(self.apply_converters(converters, rows))
```
**变异语义**：把 `apply_converters(rows, converters)` 的两个实参**顺序写反**。`apply_converters(self, rows, converters)` 的契约是第一个参数为待转换的行序列、第二个为转换器字典。传反后：`converters`（一个 dict）被当作 `rows` 来 `map(list, ...)` 迭代，而 `rows`（一个 list）被当作 converters 去 `.items()`——后者会 `AttributeError`/前者会按 dict 键迭代，转换在自定义字段的 create 路径上抛错，F2P 失败。其余 14 个测试不触发该应用分支（无 `from_db_value` 则 `converters` 为空，`if converters:` 不进入），全部通过。这是真实开发者极易犯的"参数顺序记反"错误：调用处 `rows`/`converters` 两个局部变量都在上下文里，名字都合理，单看调用行很难判断谁该在前。属 A2（函数调用参数顺序/签名契约违反）。

### Group B — 替换
**原 mutation**：
```diff
-        if converters:
+        if not converters:
```
**分类**：🔴 必须替换（取反，不自然 + 功能等价失效）
**理由**：把条件取反成"没有转换器时才应用转换器"，逻辑自相矛盾（无转换器时 `apply_converters` 拿空 dict 等于空转），正常有转换器的场景反而跳过——等价于转换永不生效，且 `if not converters:` 紧跟 `converters = get_converters(...)` 读来明显错乱。

**最终 mutation**：
```diff
-        if converters:
+        if converters and len(rows) > 1:
             rows = list(self.apply_converters(rows, converters))
```
**变异语义**：给应用条件加上 `len(rows) > 1` 的边界守卫，伪装成"只有多行返回时才需要批量套用转换器"的合理优化。但单条 `create()` 在 SQLite 上走 `last_insert_id` 分支，`rows == [(pk,)]`，`len(rows) == 1`，`1 > 1` 为假 → 跳过转换，`id` 仍是裸 int，F2P（断言为 `MyWrapper`）失败。审查者看到 `len(rows) > 1` 容易理解成"批量插入才需处理"，却忽略了**单行插入同样需要转换**这一主路径。属 B1（off-by-one 边界条件：应为"非空即处理"却写成"多于一行才处理"，把单行边界排除）。

### Group C — 替换
**原 mutation**：
```diff
-        if converters:
-            rows = list(self.apply_converters(rows, converters))
+#         if converters:
+#             rows = list(self.apply_converters(rows, converters))
```
**分类**：🔴 必须替换（字节级逆操作 + 注释痕迹）
**理由**：直接把 golden 新增的两行用 `#` 注释掉，是 patch 的逆操作；行首 `#` 缩进错位的注释是显眼的人工痕迹，审查必现。

**最终 mutation**：
```diff
         if converters:
-            rows = list(self.apply_converters(rows, converters))
+            rows = self.apply_converters(rows, converters)
         return rows
```
**变异语义**：去掉 `list(...)`，直接把 `apply_converters` 的返回值赋给 `rows`。但 `apply_converters` 是**生成器函数**（函数体含 `yield`），不物化时 `rows` 变成一个未求值的 generator 对象，`return rows` 返回它。调用方 `Model._save_table` 执行 `zip(results[0], returning_fields)`——`results` 此时是 generator，`results[0]` 因 generator 不支持下标而 `TypeError`，自定义字段 create 路径报错，F2P 失败。其余测试不进入该分支（converters 空），返回值仍是原 `rows` 列表，正常通过。这是非常自然的"去掉看似多余的 `list()`"性能直觉错误——审查者若不知道 `apply_converters` 是生成器、且下游依赖可下标列表，很难发现。属 C1（破坏隐式数据形态：list ↔ generator，惰性求值改变返回值契约）。

### Group D — 替换
**原 mutation**：
```diff
-        self.returning_fields = returning_fields
+        # self.returning_fields = returning_fields
```
**分类**：🔴 必须替换（破坏前置赋值 + 注释痕迹）
**理由**：注释掉 `self.returning_fields` 的赋值，使后续 `if not self.returning_fields:` 等多处读到旧值/缺失属性，影响范围超出转换逻辑本身；且 `#` 注释是显眼人工痕迹。

**最终 mutation**：
```diff
-        if converters:
+        if converters and self.connection.features.can_return_columns_from_insert:
             rows = list(self.apply_converters(rows, converters))
```
**变异语义**：把转换应用门控在后端特性 `can_return_columns_from_insert` 上，伪装成"只有当后端能从 INSERT 直接返回列时才需要应用转换器"的合理判断。但 SQLite 该特性为 `False`，单条 `create()` 实际走的是 `last_insert_id` 的 else 分支——此分支同样返回了需要转换的主键值，却被这个特性门控排除，转换不应用，F2P 失败。该条件名字极具迷惑性：它确实是同函数上文用过的真实特性标志，看起来"语义相关"，但实际上**转换应用与该特性正交**（无论哪条返回分支都需要转换）。属 D4（环境/后端能力依赖：把无条件逻辑错误耦合到某个后端特性开关，在不具备该特性的环境下静默失效）。

### Group E — 保留
**原 mutation**：
```diff
-    def execute_sql(self, returning_fields=None):
+    def execute_sql(self, returning_fields=None, apply_converters_on_insert=False):
...
-        if converters:
+        if converters and apply_converters_on_insert:
             rows = list(self.apply_converters(rows, converters))
```
**分类**：🟢 保留
**理由**：新增一个默认 `False` 的关键字参数 `apply_converters_on_insert` 作为开关，把"插入时应用转换器"从无条件行为变成需显式 opt-in。所有现有调用方（`QuerySet._insert` → `execute_sql(returning_fields)`）都不传该参数，于是默认走"不应用"分支，转换被静默跳过，F2P 失败。这看起来像一次合理的"提供可选开关、保持向后兼容"的接口增强，签名扩展、默认值、条件门控都符合常见代码风格，逐行审查只会觉得是个无害的特性旗标，而不会意识到**默认值把正确行为关掉了**。修改位于函数签名 + 条件两处，跨越接口契约维度，属 E2（隐式行为被改为显式参数门控，默认值使其失效），予以保留。

## 新设计 Mutation 说明

四个新设计变异（A/B/C/D）均基于对调用链与数据流的深层理解，分别攻击不同语义维度，互不重叠，且都规避了原 mutation 的人工痕迹（无 `#` 注释、无空列表硬编码、无取反矛盾）：

- **A（A2）**：`apply_converters` **实参顺序写反**。利用该方法对 `(rows, converters)` 顺序的强契约——真实开发者记反参数顺序的高频错误，调用行单看无明显破绽。
- **B（B1）**：加 `len(rows) > 1` 边界守卫。利用 SQLite 单条插入 `rows` 长度恒为 1 的事实，把单行主路径排除在转换之外，伪装成"批量才需处理"的边界优化。
- **C（C1）**：去掉 `list()`。利用 `apply_converters` 是生成器、而下游 `results[0]` 需可下标列表的隐式形态契约，伪装成去除冗余物化的性能直觉。
- **D（D4）**：用 `can_return_columns_from_insert` 后端特性门控。利用 SQLite 该特性为 `False` 且转换逻辑与之正交的事实，伪装成"按后端能力分流"的合理判断。

全部仅修改 `django/db/models/sql/compiler.py`（允许文件），不触碰测试文件。均通过 Step 5 实证自查：在 base_commit → golden patch → test_patch 之后用 `git diff HEAD` 生成、可干净应用、`py_compile` 通过，并实际运行整个 `custom_pk.tests`（15 个测试）确认每个变异都**只**使 F2P 测试 `test_auto_field_subclass_create` 失败（A/C 为 ERROR，B/D/E 为断言 FAILURE），其余 14 个测试全部通过（`test_auto_field_subclass_bulk_create` 在 SQLite 上按 `@skipUnlessDBFeature` 跳过，无附带破坏）。
