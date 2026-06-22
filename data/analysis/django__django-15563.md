# django__django-15563

## 问题背景

多重继承（MTI）下 queryset update 行为错误：更新发生在错误的父类实例上。根因在 `SQLUpdateCompiler.pre_sql_setup`：对 related_updates（祖先表更新）只用单一 `idents`（子类主键）做过滤，但当某祖先不在主键链上（多父继承的非 pk-chain 父类）时，需要单独收集该祖先的主键。Golden patch 按 parent 分别收集 related_ids（`defaultdict(list)`），并在 `subqueries.py` 用 `self.related_ids[model]` 取对应父类的 ids。

## Golden Patch 语义分析

```python
fields = [meta.pk.name]
related_ids_index = []
for related in self.query.related_updates:
    if all(path.join_field.primary_key for path in meta.get_path_to_parent(related)):
        related_ids_index.append((related, 0))           # pk 链存在，复用 meta.pk
    else:
        related_ids_index.append((related, len(fields))) # 非 pk 链，单独取一列
        fields.append(related._meta.pk.name)
query.add_fields(fields)
...
related_ids = collections.defaultdict(list)
for rows in ...execute_sql(MULTI):
    idents.extend(r[0] for r in rows)
    for parent, index in related_ids_index:
        related_ids[parent].extend(r[index] for r in rows)
self.query.related_ids = related_ids
```
核心语义：**对每个祖先更新，判断是否在主键链上：在则用主键列（index 0），不在则额外 SELECT 该祖先主键列（index=len(fields)）；执行后按 parent 分别收集 ids，存为按 model 索引的 dict**。`subqueries.py` 的 `get_related_updates` 用 `self.related_ids[model]` 取对应父类 ids。多处协同：`all` 判定、index 计算、defaultdict 收集、按 model 取值。

F2P 测试 `test_mti_update_parent_through_child` 与 `test_mti_update_grand_parent_through_child`：通过子类 update 祖先字段，断言更新落在正确实例上。

## 调用链分析

`pre_sql_setup`：对每个 related update 算 `(related, index)`，执行子查询后按 index 从结果行取对应列填入 `related_ids[parent]`。`subqueries.get_related_updates` 用 `self.related_ids[model]` 构造各父类的 `pk__in` 过滤。`all(...primary_key)` 决定走哪个分支与用哪个列。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `all`→`any`，pk-chain 判定放宽，非 pk-chain 父类误判为可复用主键 |
| B | 🔴 必须替换 | 替换 | 原 A=B 重复（同 `all`→`any`）；改为收集时 `r[index]`→`r[0]` |
| C | 🟢 高质量 | 保留 | `defaultdict(list)`→`[]`，按 model 索引时 TypeError |
| D | 🟢 高质量 | 保留 | else 分支 index `len(fields)`→`0`，非 pk-chain 父类取错列 |
| E | 🟢 高质量 | 保留 | related_ids 退回 idents（除非开关），按 model 取值失效 |

原 A=B 重复。重做 B 为收集列错误。

## 各组 Mutation 分析

### Group A — 保留（B3 边界：all→any）
```diff
-            if all(
+            if any(
                 path.join_field.primary_key for path in meta.get_path_to_parent(related)
             ):
```
**变异语义**：把"路径上**所有** join 都是主键"放宽成"**任一** join 是主键"。非 pk-chain 的祖先（部分路径是主键）被误判为可复用 meta.pk（走 index 0 分支），不再额外取该祖先主键列，导致用错误的 id 更新祖先。模拟 all/any 混淆。保留。

### Group B — 替换（B1 边界：收集列索引错）
**原**：与 A 重复。
**最终 mutation**：
```diff
                 for parent, index in related_ids_index:
-                    related_ids[parent].extend(r[index] for r in rows)
+                    related_ids[parent].extend(r[0] for r in rows)
```
**变异语义**：收集 related_ids 时无视计算好的 `index`，一律取 `r[0]`（子类主键列）。非 pk-chain 父类本应取自己额外 SELECT 的列（index>0），却拿到子类主键值，更新错位。模拟"硬编码索引 0、忽略 related_ids_index 的设计"。

### Group C — 保留（C1 类型/数据形状）
```diff
-            related_ids = collections.defaultdict(list)
+            related_ids = []
```
**变异语义**：`related_ids` 用 list 而非 `defaultdict(list)`。后续 `related_ids[parent].extend(...)` 对 list 用 model 作下标 → TypeError；且 `subqueries.py` 的 `self.related_ids[model]` 同样失败。数据结构形状错误。保留。

### Group D — 保留（B1 边界：else 索引错）
```diff
                 # ancestor that is not part of the primary key chain of a MTI tree.
-                related_ids_index.append((related, len(fields)))
+                related_ids_index.append((related, 0))
                 fields.append(related._meta.pk.name)
```
**变异语义**：非 pk-chain 分支仍额外 append 了主键列到 fields，但记录的 index 错成 0（指向子类主键列）而非 `len(fields)`（指向新加的祖先主键列）。收集时取错列，祖先用错误 id 更新。模拟"index 计算 off-by"。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
-            self.query.related_ids = related_ids
+            self.query.related_ids = related_ids if getattr(self.query, "use_related_ids_dict", False) else idents
```
**变异语义**：把"使用按 model 分组的 dict"藏到 `use_related_ids_dict` 开关后，默认 False → 退回旧的 `idents`（单一子类主键列表）。`subqueries.py` 期望 `related_ids[model]`，对 list 用 model 下标失败/取错。模拟"把修复做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

原 A=B 重复（都 `all`→`any`）。本次保留 A（all→any）、C（list 替 defaultdict）、D（else 索引错）、E（默认退回 idents），把重复的 B 改为"收集时取 `r[0]` 而非 `r[index]`"。五组覆盖"pk-chain 判定 / 收集列索引 / 容器类型 / index 计算 / 默认开关"五个角度，跨 compiler.py 的多个协同点。全部实测：golden 通过、两个 F2P 测试均失败、`base→golden→test_patch` 后干净应用。
