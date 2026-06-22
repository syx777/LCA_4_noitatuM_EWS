# django__django-14672

## 问题背景

Django 3.2 给所有 `ForeignObjectRel` 增加了 `identity` 属性以支持比较，`__hash__` 由 `identity`（一个 tuple）派生。`identity` 中的不可哈希元素（如 `limit_choices_to`）通过 `make_hashable` 规整。但 `ManyToManyRel.identity` 里的 `through_fields` 可能是 list（如 `through_fields=['child','parent']`），却漏掉了 `make_hashable` 调用，导致对该关系求 hash 时报 `TypeError: unhashable type: 'list'`。该问题在 proxy model 检查时暴露（proxy 有更多检查触发了 hash）。Golden patch 在 `ManyToManyRel.identity` 中对 `through_fields` 补上 `make_hashable`。

## Golden Patch 语义分析

```python
@property
def identity(self):
    return super().identity + (
        self.through,
        make_hashable(self.through_fields),   # 原为 self.through_fields
        self.db_constraint,
    )
```
核心语义：**`identity` 必须完全可哈希**，因为 `__hash__ = hash(self.identity)`。`make_hashable` 把 list 递归转成 tuple，使 `through_fields=['event','invitee']` 这类列表也能进入哈希。修复保证：(1) 含 list `through_fields` 的 m2m 关系可被 hash（model 系统检查不再崩溃）；(2) 内容相同的 `through_fields`（即便分别来自父模型与 proxy/继承子模型）哈希相等。

F2P 测试：`test_field_name_clash_with_m2m_through`（带 list `through_fields` 的 m2m 在 model check 时不崩溃，正常报字段冲突 E006）、`test_reverse_inherited_m2m_with_through_fields_list_hashable`（Person 与继承子类 PersonChild 的反向 m2m 字段 `through_fields` 相等且 hash 相等）。

## 调用链分析

`ForeignObjectRel.__hash__`（基类）返回 `hash(self.identity)`。`ManyToManyRel` 覆写 `identity` 属性，把 `through`、`through_fields`、`db_constraint` 追加到 `super().identity`。当 model 系统做字段检查（尤其 proxy/继承模型）时会对 rel 求 hash → 触发 `hash(identity)` → 若 `through_fields` 是未规整的 list 则 `hash(tuple(...,list,...))` 抛 `TypeError`。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| C | 🔴 必须替换 | 替换 | 原 diff 改的是基类 `__hash__`，加了 `hasattr/isinstance` 特判返回 `id(self)`，并带 "Bug:" 注释——人工痕迹明显、且改错了位置（不在 golden 修复的 `identity` 上） |

仅有 C 一组，必须替换为自然的高质量变异。

## 各组 Mutation 分析

### Group C — 替换
**原 mutation**：
```diff
     def __hash__(self):
+        # Bug: Skip make_hashable for through_fields lists in ManyToManyRel
+        if hasattr(self, "through_fields") and isinstance(self.through_fields, list):
+            return id(self)  # Use object id instead of identity
         return hash(self.identity)
```
**分类**：🔴 必须替换（注释直写 "Bug"、硬编码 `id(self)`、改的是基类而非 golden 修复点，代码审查一眼可见）
**最终 mutation**：
```diff
@@ class ManyToManyRel.identity
         return super().identity + (
             self.through,
-            make_hashable(self.through_fields),
+            make_hashable(self.through_fields) if isinstance(self.through_fields, tuple) else self.through_fields,
             self.db_constraint,
         )
```
**变异语义**：在 golden patch 修复的同一处，把 `make_hashable` 限定为"仅当 `through_fields` 是 tuple 时才规整"。这看似是一个无害的"优化/防御"——开发者假设 `through_fields` 通常已是 tuple，tuple 本就可哈希，于是只在 tuple 分支调用 `make_hashable`。但当 `through_fields` 是 **list**（正是本 issue 的触发条件）时走 else 分支，原样放入 identity，`hash(self.identity)` 再次抛 `TypeError: unhashable type: 'list'`——精确复现原始 bug，但伪装成一个"类型分支优化"。`test_field_name_clash_with_m2m_through`（list through_fields）因 hash 崩溃而失败。
**为何难发现**：表面上 `make_hashable` 调用仍在、修复"看起来还在"；`isinstance(..., tuple)` 读起来像合理的类型守卫；只有理解到"list 才是问题来源、而它恰好被排除在规整之外"才能识破。通过编译，仅令 F2P 失败。

## 新设计 Mutation 说明

替代保持在 golden 修复点（`ManyToManyRel.identity`），用一个"类型形状假设"（C 组主题：Type & Data Shape）削弱修复：只规整 tuple、放过 list。这比原 mutation（改基类 + "Bug" 注释 + `id(self)`）自然得多，且语义上正中 issue 要害——list 型 `through_fields` 不可哈希。实测在 `base→golden→test_patch` 后干净应用、`py_compile` 通过，运行 F2P 时以 `unhashable type: 'list'` 失败。
