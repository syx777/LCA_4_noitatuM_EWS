# django__django-14725

## 问题背景

Model formset 没有提供"仅编辑（edit-only）"视图：用户想禁止通过 formset 新建对象，常误用 `extra=0`，但这并不可靠（可用 JS 或额外 POST 数据再加表单）。Golden patch 为 `modelformset_factory` / `inlineformset_factory` 增加 `edit_only` 参数，并在 `BaseModelFormSet.save()` 中：当 `edit_only` 为真时只保存已存在对象（`save_existing_objects`），跳过 `save_new_objects`。

## Golden Patch 语义分析

核心语义是**在保存阶段按 `edit_only` 分流**：
```python
if self.edit_only:
    return self.save_existing_objects(commit)
else:
    return self.save_existing_objects(commit) + self.save_new_objects(commit)
```
并通过工厂函数把 `edit_only` 一路透传：`modelformset_factory(... edit_only=False)` → `FormSet.edit_only = edit_only`；`inlineformset_factory` 把 `edit_only` 放进 kwargs 再交给 `modelformset_factory`。因此正确性依赖三处协同：(1) save 分支判断、(2) modelformset 工厂赋值、(3) inline 工厂透传。

F2P 测试覆盖三个场景：`test_edit_only`（modelformset 不新建）、`test_edit_only_inlineformset_factory`（inline 不新建）、`test_edit_only_object_outside_of_queryset`（edit-only 下提交不在 queryset 中的对象不会被改动/新建）。

## 调用链分析

`save()` → `save_existing_objects()` / `save_new_objects()`。`save_new_objects()` 遍历 `self.extra_forms`，对每个已改动且未标记删除的表单调用 `save_new()` 落库。`edit_only` 属性由工厂在类上设置（`FormSet.edit_only = edit_only`），实例通过 `self.edit_only` 读取。`initial_form_count()` 来自 `BaseFormSet`，表示已存在记录的表单数；`commit` 参数控制是否真正写库（save 默认 `commit=True`）。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原 diff 与 B 字节级完全相同（`if self.edit_only`→`if not self.edit_only`），重复 |
| B | 🔴 必须替换 | 替换 | 与 A 重复，且为简单逻辑反转，过于浅层 |
| C | 🔴 必须替换 | 替换 | `str(edit_only)` 使值恒为真（"False" 也是非空串），连正常 add 场景都坏，blast radius 大且不自然 |

语义浅层共 0 个；三个均必须替换，故各设计一个高质量替代（跨站点/契约/状态分流）。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**（与 B 相同）：
```diff
-        if self.edit_only:
+        if not self.edit_only:
```
**分类**：🔴 必须替换（与 B 重复 + 浅层反转）
**最终 mutation**：
```diff
@@ inlineformset_factory kwargs
-        'edit_only': edit_only,
+        'edit_only': False,
```
**变异语义**：inline 工厂在向 `modelformset_factory` 透传 kwargs 时把 `edit_only` 硬写为 `False`，使 `inlineformset_factory(..., edit_only=True)` 的意图被悄悄丢弃。modelformset 路径（直接调 `modelformset_factory`）完全正常，只有 inline 路径失效——`test_edit_only_inlineformset_factory` 失败，另两个测试通过。模拟"复制参数字典时手误填默认值"的真实错误，全 suite 仅 1 个失败，极难察觉。

### Group B — 替换
**最终 mutation**：
```diff
-        if self.edit_only:
+        if self.edit_only and self.initial_form_count():
```
**分类**：🔴 必须替换
**变异语义**：把"是否进入 edit-only 分支"额外绑定到"是否已有初始表单"。当 `INITIAL_FORMS=0`（无已存在记录，全是新表单）时 `initial_form_count()` 为 0，条件为假，于是仍会执行 `save_new_objects` 新建对象——正是 `test_edit_only` 第一段（INITIAL_FORMS=0、提交两个新名字）所断言不应发生的。看似"edit-only 只在有现存对象时才有意义"的合理化防御，实则违反契约。

### Group C — 替换
**原 mutation**：`is True` + 两处 `str(edit_only)`（恒真，正常场景也坏）。
**分类**：🔴 必须替换（功能过广 + 不自然）
**最终 mutation**：
```diff
-        if self.edit_only:
+        if self.edit_only and not commit:
```
**变异语义**：把 edit-only 抑制新建的行为错误地限定为"仅在 `commit=False` 时生效"。由于 `save()` 默认 `commit=True`，真实保存路径下永远走 else 分支照常新建，而 `save(commit=False)` 延迟保存时才"正确"。模拟开发者把 edit_only 与 commit 语义混淆。三个 F2P 测试均以默认 commit 保存，故失败。

## 新设计 Mutation 说明

三个替代分别作用于**不同站点与机制**，互不重复：A 在 inline 工厂的参数透传层（跨函数状态传播）、B 在 save 分支引入与 `initial_form_count` 的伪相关（条件组合），C 把 edit_only 错误耦合到 `commit` 参数（接口契约误解）。三者都能通过编译、在 `base→golden→test_patch` 后干净应用，且实测仅令对应 F2P 测试失败、对完整 model_formsets 套件的附带破坏极小（A/B 各 1 个失败，C 2 个）。
