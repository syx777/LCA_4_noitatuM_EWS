# django__django-16100

## 问题背景

Django admin changelist 的 `list_editable` 批量保存没有事务包裹。若保存多行时中途出错（如 `log_change` 抛 `DatabaseError`），已保存的行不会回滚，数据库进入不一致状态。Golden patch 把 changelist_view 中遍历 formset、逐行 `save_form/save_model/save_related/log_change` 的循环用 `with transaction.atomic(using=router.db_for_write(self.model))` 包裹，使整批保存要么全成功要么全回滚。

## Golden Patch 语义分析

```python
if formset.is_valid():
    changecount = 0
    with transaction.atomic(using=router.db_for_write(self.model)):
        for form in formset.forms:
            if form.has_changed():
                obj = self.save_form(request, form, change=True)
                self.save_model(request, obj, form, change=True)
                self.save_related(request, form, formsets=[], change=True)
                change_msg = self.construct_change_message(request, form, None)
                self.log_change(request, obj, change_msg)
                changecount += 1
```
核心语义：**整个批量编辑循环必须在一个原子事务内执行**。`router.db_for_write(self.model)` 选出正确的写库；`transaction.atomic` 保证循环中任一步（含 `log_change`）抛异常时，本批所有已执行的 save 全部回滚。修复的三要素：(1) 事务真正包裹循环；(2) 用正确的数据库别名；(3) 异常能向上传播触发回滚（不能被吞掉）。

F2P 测试 `ChangeListTests.test_list_editable_atomicity`：mock `log_change` 抛 `DatabaseError`，POST 批量编辑，断言抛 `DatabaseError` 且两行原值都被保留（回滚成功）。

## 调用链分析

`changelist_view` 在 formset 校验通过后遍历 forms 逐行保存。`save_form`/`save_model` 写主对象，`save_related` 写关联，`log_change` 记日志。任一步抛异常时：有 `atomic` 包裹 → 整批回滚；无包裹 → 已 save 的行留在库里。`transaction.atomic(using=...)` 的 `using` 必须是 `router.db_for_write(self.model)` 返回的数据库别名（字符串）；传错（如传 model 对象）会导致事务作用在错误/无效的连接上。异常必须能逃出 `atomic` 块才能触发回滚——若被 try/except 吞掉，`atomic` 正常退出并提交。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | try/except 吞掉异常，atomic 正常提交不回滚 |
| B | 🟢 高质量 | 保留 | `has_changed()`→`not has_changed()`，保存逻辑反转 |
| C | 🟢 高质量 | 保留 | `using=router.db_for_write(self.model)`→`using=self.model`，事务用错数据库别名 |
| D | 🟢 高质量 | 保留 | 删除 atomic 包裹，还原无事务 bug |
| E | 🟢 高质量 | 保留 | 用 nullcontext 替代 atomic，藏到默认关闭开关后 |

五组机制各异且均有效，全部保留（仅核验）。

## 各组 Mutation 分析

### Group A — 保留（D2 状态：吞异常阻止回滚）
```diff
-                with transaction.atomic(using=router.db_for_write(self.model)):
-                    for form in formset.forms:
-                        if form.has_changed():
-                            ...
+                try:
+                    with transaction.atomic(using=router.db_for_write(self.model)):
+                        for form in formset.forms:
+                            if form.has_changed():
+                                ...
+                except Exception:
+                    pass
```
**变异语义**：在 atomic 块外裹一层 `try/except Exception: pass`。异常被吞 → 但注意 `transaction.atomic` 在 `__exit__` 检测到异常时会标记回滚；然而异常被外层捕获后视图不再抛 `DatabaseError`。F2P 断言"抛 DatabaseError"失败（异常被吞，无法传播）。模拟"加了宽泛的 try/except 兜底、意外吞掉了该传播的错误"。保留。

### Group B — 保留（B3 条件反转）
```diff
                     for form in formset.forms:
-                        if form.has_changed():
+                        if not form.has_changed():
```
**变异语义**：保存条件反转——只保存**未改动**的 form，跳过改动的。批量编辑实际不保存用户的修改，逻辑完全错误。F2P 的保存流程被破坏。保留。

### Group C — 保留（A1 接口契约：错误的 using 参数）
```diff
-        with transaction.atomic(using=router.db_for_write(self.model)):
+        with transaction.atomic(using=self.model):
```
（同时作用于 changeform_view / changelist_view / delete_view 三处）
**变异语义**：`transaction.atomic(using=...)` 的 `using` 期望数据库别名字符串（如 `"default"`），却传了 `self.model`（模型类）。`router.db_for_write(self.model)` 才返回别名。传模型类会让事务管理用错误的 key 查连接，行为异常/报错。模拟"以为 using 接受 model、漏了 router.db_for_write 解析"。保留。

### Group D — 保留（B2 删除事务包裹）
```diff
-                with transaction.atomic(using=router.db_for_write(self.model)):
-                    for form in formset.forms:
-                        if form.has_changed():
-                            ...
+                for form in formset.forms:
+                    if form.has_changed():
+                        ...
```
**变异语义**：删除 `atomic` 包裹，循环裸跑。中途 `log_change` 抛错时已保存的行不回滚，还原原始无事务 bug。F2P 断言原值保留失败（部分行已被改写）。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
+        self.use_atomic_transactions = False
...
-                with transaction.atomic(using=router.db_for_write(self.model)):
+                if self.use_atomic_transactions:
+                    context_manager = transaction.atomic(using=router.db_for_write(self.model))
+                else:
+                    from contextlib import nullcontext
+                    context_manager = nullcontext()
+                with context_manager:
```
**变异语义**：新增实例属性 `use_atomic_transactions`（默认 False），默认用 `nullcontext()`（无事务）替代 `atomic`。只有显式设 True 才启用事务。默认情况下等于无事务保护，回滚失效。模拟"把事务做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

本实例原五组机制已各不相同且全部有效，无重复、无必须替换项，故全部保留并逐一核验。五组覆盖"吞异常阻止回滚 / 保存条件反转 / 错误 using 参数 / 删除事务 / 默认关闭开关"五个角度，分别破坏修复的不同要素（异常传播、保存逻辑、数据库别名、事务存在性、默认启用）。全部实测：golden 通过、五个变异均令 F2P（`test_list_editable_atomicity`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
