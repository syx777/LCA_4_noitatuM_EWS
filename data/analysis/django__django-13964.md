# django__django-13964

## 问题背景

当一个外键指向的父模型使用**非自增的 CharField 作为主键**时，若先把未保存的空父对象赋给子对象、之后再设置父对象主键并保存，子对象的外键列会被写成空字符串 `''` 而非父对象真正的主键，导致数据丢失（外键约束在事务提交时才报错）。

根因：`_prepare_related_fields_for_save` 在保存子对象前，会把子对象的外键属性（`field.attname`，如 `product_id`）补成关联对象的 pk，但旧代码只在该属性 `is None` 时才补。对于 CharField 主键，未赋值时的"空"值是空字符串 `''` 而不是 `None`，于是补救逻辑被跳过。Golden patch 把判断从 `is None` 改为 `in field.empty_values`，使空字符串也被识别为"需要补 pk"。

## Golden Patch 语义分析

```python
elif getattr(self, field.attname) in field.empty_values:
    # Use pk from related object if it has been saved after an assignment.
    setattr(self, field.attname, obj.pk)
```

- `field.empty_values` 是 `[None, '', [], (), {}]`（来自 `validators.EMPTY_VALUES`）。
- 修复的核心：把"外键列尚未被有效赋值"的判定从仅 `None` 扩展到所有空值，**关键是纳入空字符串 `''`**——这正是 CharField 主键的默认空态。
- 当外键列处于任一空值时，用关联对象此刻的真实 pk（`obj.pk`）回填。

F2P 测试 `test_save_fk_after_parent_with_non_numeric_pk_set_on_child`：父模型 `ParentStringPrimaryKey` 用 `name = CharField(primary_key=True)`；先 `child = ChildStringPrimaryKeyParent(parent=parent)`（此时 `child.parent_id == ''`），再设 `parent.name='jeff'`、保存父、保存子，断言 `child.parent_id == 'jeff'`。只要回填逻辑不把 `''` 当空值处理，子对象的 `parent_id` 就停留在 `''`，断言失败。

## 调用链分析

- `_prepare_related_fields_for_save` 由 `Model.save_base` → `save` 流程在写库前调用，遍历 `concrete_fields`。
- 对每个已缓存的关系字段：若关联对象 `obj.pk is None` 则报错防数据丢失；否则若子对象自身的外键列 `attname` 为空，则回填 `obj.pk`。
- 数据流关键区分：
  - **数值主键场景**（兄弟测试 `test_save_nullable_fk_after_parent` 等）：外键列未赋值时为 `None`，必须仍被当作空值回填。
  - **字符串主键场景**（F2P）：外键列未赋值时为 `''`，这是 golden patch 新覆盖的情形。
- 因此任何变异只要**让 `''` 不再被判为空值**（同时保持 `None` 仍判为空），就会只让 F2P 失败而不波及数值主键的兄弟测试——这正是本组所有高质量变异的设计支点。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原 mutation 把 `in field.empty_values` 改回 `is None`，是 golden patch 字节级逆操作，直接冗余 |
| B | 🔴 必须替换 | 替换 | 把条件整体取反（`not in`），逻辑明显错乱（关联对象有 pk 时反而不回填），且与修复语义无关，属不自然 |
| C | 🔴 必须替换 | 替换 | 同 A，字节级还原为 `is None`，直接冗余 |
| D | 🔴 必须替换 | 替换 | 加 `field.attname not in self.__dict__` 前置守卫，效果上恢复旧行为且写法不自然 |
| E | 🔴 必须替换 | 替换 | 同 A，字节级还原为 `is None`，直接冗余 |

> A/C/E 三份完全相同（还原为 `is None`）；B 取反；D 加守卫。5 个全部 🔴 必须替换。语义浅层 0 个，全部替换为高质量变异。

## 各组 Mutation 分析

所有最终变异的共同机制：**把"判定为空值"的集合悄悄收窄，使空字符串 `''` 不再算空**（从而 CharField 主键场景不回填 → F2P 失败），但仍保留 `None`（数值主键兄弟测试不受影响）。五个组用五种不同写法伪装这一收窄。

### Group A — 替换（原：还原为 `is None`，直接冗余）
**最终 mutation**：
```diff
-                elif getattr(self, field.attname) in field.empty_values:
+                elif getattr(self, field.attname) in (None, 0):
```
**变异语义**：把空值集合写成 `(None, 0)`——看起来像是"处理未赋值（None）和数值主键为 0 这两种待回填情形"的合理特化。但它丢掉了 `''`：字符串主键场景下 `parent_id == ''` 不在 `(None, 0)` 中，不回填，F2P 失败。`0` 这个元素很有迷惑性，让人以为作者考虑了数值边界，反而忽略了字符串空态。属 A1（改写参数/取值语义）。

### Group B — 替换（原：整体取反，不自然）
**最终 mutation**：
```diff
-                elif getattr(self, field.attname) in field.empty_values:
+                elif getattr(self, field.attname) in field.empty_values[:1]:
```
**变异语义**：用切片 `empty_values[:1]` 只取列表第一个元素。`field.empty_values == [None, '', [], (), {}]`，`[:1]` 即 `[None]`，于是等价于"只认 `None` 为空"，但写法上保留了 `empty_values` 的引用，**伪装成仍在用官方空值列表**。审查者看到 `field.empty_values` 容易放心，却忽略了 `[:1]` 把列表截断到只剩 `None`，空字符串被排除，F2P 失败。属 B1（切片边界 off-by：本应取全部却只取首个）。

### Group C — 替换（原：还原为 `is None`，直接冗余）
**最终 mutation**：
```diff
-                elif getattr(self, field.attname) in field.empty_values:
+                elif getattr(self, field.attname) in (None, [], (), {}):
```
**变异语义**：把空值集合手写成 `(None, [], (), {})`——几乎完整复刻了 `EMPTY_VALUES`，唯独漏掉了空字符串 `''`。这种"手抄常量但漏一项"的写法极其隐蔽：四个空容器都在，看起来覆盖周全，但字符串主键的空态恰恰是被漏掉的那个 `''`，导致不回填，F2P 失败。属 C1（破坏对空字符串的隐式归一化处理）。

### Group D — 替换（原：加 `__dict__` 守卫，不自然）
**最终 mutation**：
```diff
-                elif getattr(self, field.attname) in field.empty_values:
+                elif vars(self).get(field.attname) is None:
```
**变异语义**：改用 `vars(self).get(field.attname)` 直接读实例 `__dict__`，并判 `is None`。`vars(self).get(...)` 在属性已设值时返回该值（`''`），其 `is None` 为假；只有当 `__dict__` 中根本没有该键时 `.get` 返回 `None` 才为真。结果是：字符串主键场景 `parent_id` 已是 `''`（存在于 `__dict__`），`.get` 返回 `''`，`'' is None` 为假，不回填，F2P 失败；而它把"读属性"从 `getattr`（会触发描述符/默认值）换成读裸 `__dict__`，看似只是性能优化或等价访问，实则改变了空值语义。属 D1（状态读取方式变化导致初始化判断不完整）。

### Group E — 替换（原：还原为 `is None`，直接冗余）
**最终 mutation**：
```diff
-                elif getattr(self, field.attname) in field.empty_values:
+                elif getattr(self, field.attname) == None:
```
**变异语义**：用 `== None` 替代 `in field.empty_values`。表面上像是把"是否为空"的判断简化成最常见的"是否为 None"，符合很多开发者的直觉写法（甚至 `== None` 而非 `is None` 更像随手而就）。但它只匹配 `None`，空字符串 `''` 不相等，字符串主键场景不回填，F2P 失败。这是把精确的空值契约偷换成单一的 None 判等，破坏了测试对"空字符串也应回填"的精确期望。属 E1（改变行为使精确断言失效）。

## 新设计 Mutation 说明

五个变异都作用于同一行判空条件，但用五种互不重叠的伪装手法收窄空值集合、排除 `''`：
- **A**：写成 `(None, 0)`，用数值 0 转移注意力。
- **B**：`empty_values[:1]` 切片截断，保留官方常量引用作掩护。
- **C**：手抄 `(None, [], (), {})` 漏掉 `''`，伪装成完整覆盖。
- **D**：改读 `vars(self).__dict__` 并判 `is None`，伪装成等价访问。
- **E**：`== None` 单一判等，伪装成直觉化简化。

全部仅修改 `django/db/models/base.py`（允许文件），不触碰测试。均通过 Step 5 实证自查：base_commit → golden patch → test_patch 后可干净应用、`py_compile` 通过，并实际运行整个 `ManyToOneTests`（37 个测试）确认每个变异都**只**使 F2P 测试 `test_save_fk_after_parent_with_non_numeric_pk_set_on_child` 失败，其余 36 个数值主键相关测试全部通过（无附带破坏）。
