# django__django-12273 Mutation 分析

## 问题背景
Bug：对多表继承（MTI）的子模型，将主键设为 `None` 无法生效——`obj.pk = None` 之后再 `save()` 仍然覆盖了原有对象，而不是创建新对象。根因在于 `Model._set_pk_val` 只设置了当前模型 `_meta.pk` 这一个属性，没有同步重置指向父表的 `parent_link`（即 `*_ptr_id`）外键字段。子模型的真实主键其实是父链外键，因此仅置空 `pk` 属性不足以让 ORM 认为这是一个新实例。

## Golden Patch 语义分析
`django/db/models/base.py` 的 `_set_pk_val`：

```python
def _set_pk_val(self, value):
    for parent_link in self._meta.parents.values():
        if parent_link and parent_link != self._meta.pk:
            setattr(self, parent_link.target_field.attname, value)
    return setattr(self, self._meta.pk.attname, value)
```

Golden 新增循环：遍历所有父链 `parent_link`，对于「存在且不是本模型 pk」的父链，把传入 `value`（典型为 `None`）写到该父链所指向的目标字段属性 `target_field.attname`（如 `user_ptr_id`、`politician_ptr_id`），从而把整条继承链的主键级联重置。三处语义要点：
1. 遍历 `_meta.parents.values()`（继承链上所有父链外键）。
2. 守卫 `parent_link != self._meta.pk`（避免对自身 pk 重复/错误赋值）。
3. 写入对象为 `parent_link.target_field.attname`（父表目标列名），而非本地外键列名。

## 调用链分析
`pk = property(_get_pk_val, _set_pk_val)` → 用户 `obj.pk = None` 触发 `_set_pk_val(None)` → 级联 `setattr` 各父链 `*_ptr_id = None` → `save()` 时 ORM 因主键全为 None 而执行 INSERT 创建新行。F2P 测试 `test_create_new_instance_with_pk_equals_none`（单层继承 Profile/User）与 `test_create_new_instance_with_pk_equals_none_multi_inheritance`（多重继承 Congressman/Person/Politician）正是断言「count==2 且原对象未被覆盖」。若级联重置失效，会因 UNIQUE 约束失败（IntegrityError）或覆盖原行而失败。

## 替换决策总览

| 组 | 原 diff | 分类 | 决策 | 最终变异 |
|----|---------|------|------|----------|
| A | `!=` → `==` | 🔴 MUST REPLACE（与 B 字节级重复，功能等价冗余） | 替换 | 写错属性：`target_field.attname` → `attname` |
| B | `!=` → `==` | 🔴 MUST REPLACE（与 A 字节级完全相同，重复） | 替换 | 加值守卫：`if value is not None:` 包裹级联循环 |

说明：两条输入 mutation 的 diff **逐字节完全相同**（都是把守卫 `parent_link != self._meta.pk` 改成 `==`）。这属于功能等价冗余/直接重复，两条都判定 🔴 必须替换。计数：ALL 🔴 = 2 条全部替换。

## 各组 Mutation 分析

### Group A（原始）
- 原 diff：`if parent_link and parent_link != self._meta.pk:` → `== self._meta.pk`
- 分类：🔴。与 Group B 完全相同，且该改动语义是「只对本模型 pk 自身做级联」——`parent_link == self._meta.pk` 几乎恒为 false 的边链组合，行为退化为完全不级联，等价于反向回到 buggy 行为，属于 golden 的直接逆操作类冗余。
- 最终 diff（已验证）：

```diff
@@ class Model(metaclass=ModelBase):
     def _set_pk_val(self, value):
         for parent_link in self._meta.parents.values():
             if parent_link and parent_link != self._meta.pk:
-                setattr(self, parent_link.target_field.attname, value)
+                setattr(self, parent_link.attname, value)
         return setattr(self, self._meta.pk.attname, value)
```

- 变异语义：把级联写入目标从「父表目标列 `target_field.attname`」错写成「父链外键自身的本地列 `attname`」。这是 ORM 字段接口契约层面的混淆——开发者容易把 `parent_link.attname`（本地 `*_ptr_id` 列）与 `parent_link.target_field.attname`（父表 pk 列）搞混。对单层继承两者恰好同名时不会出错，但在多重/多层继承的属性名不一致时导致重置写到错误属性，整条链未被正确清空，触发 UNIQUE 约束失败。

### Group B（原始）
- 原 diff：同 Group A，逐字节相同。
- 分类：🔴。重复。
- 最终 diff（已验证）：

```diff
@@ class Model(metaclass=ModelBase):
     def _set_pk_val(self, value):
-        for parent_link in self._meta.parents.values():
-            if parent_link and parent_link != self._meta.pk:
-                setattr(self, parent_link.target_field.attname, value)
+        if value is not None:
+            for parent_link in self._meta.parents.values():
+                if parent_link and parent_link != self._meta.pk:
+                    setattr(self, parent_link.target_field.attname, value)
         return setattr(self, self._meta.pk.attname, value)
```

- 变异语义：在级联循环外加一层 `if value is not None:` 守卫。开发者凭直觉认为「只有给定真实主键值时才需要同步父链」，从而漏掉了最关键的 `value is None`（重置）场景。常规赋值（`obj.pk = <int>`）行为不变，但本 issue 的核心用例 `obj.pk = None` 会跳过整条父链重置，父链 `*_ptr_id` 仍保留旧值，`save()` 覆盖/冲突，F2P 失败。这是 init/reset 边界条件建模错误，与 Group A 的「写错属性」失败模式正交。

## 新设计 Mutation 说明（正交性）
- Group A（接口契约错误）：选错赋值目标属性，循环结构与守卫均完整保留，对单层同名继承可通过、对名称不一致的多继承失败 → 失败维度是「赋值目标」。
- Group B（边界条件错误）：循环结构与赋值目标都正确，但被多余的非空守卫挡住 reset 路径 → 失败维度是「触发条件」。
两者从不同语义维度破坏同一函数，难以被同一条测试断言或同一种推理同时覆盖，保证多样性。

## 验证结果（REAL）
- 测试模块：`model_inheritance_regress.tests.ModelInheritanceTest`
- F2P：`test_create_new_instance_with_pk_equals_none`、`test_create_new_instance_with_pk_equals_none_multi_inheritance`
- Baseline（golden 无变异）：单测 2 passed（`OK`）；全模块 30 tests `OK (expected failures=1)`。
- Group A 变异：F2P 2 errors（IntegrityError: UNIQUE constraint failed），全模块仅这 2 个 F2P 报错，`expected failures=1` 不变，无 P2P 回归。
- Group B 变异：F2P 2 errors（UNIQUE constraint failed: profile.user_ptr_id / congressman.politician_ptr_id），全模块仅这 2 个 F2P 报错，无 P2P 回归。
- 两条最终 diff 均：可 `patch -p1` 应用于 golden+test_patch 后的 POST-PATCH 内容、`py_compile` 通过、F2P 失败、P2P 全绿。
