# django__django-16333

## 问题背景

`UserCreationForm`（继承 `ModelForm`）用于带 ManyToManyField 的自定义 User 模型时，M2M 字段数据不被保存。因为它重写的 `save(commit=True)` 调了 `user.save()` 但漏调 `self.save_m2m()`——而父类 `ModelForm` 在 commit 时会调 `save_m2m` 来保存延迟的多对多关系。Golden patch 在 `user.save()` 后补上 `if hasattr(self, "save_m2m"): self.save_m2m()`。

## Golden Patch 语义分析

```python
def save(self, commit=True):
    user = super().save(commit=False)
    user.set_password(self.cleaned_data["password1"])
    if commit:
        user.save()
        if hasattr(self, "save_m2m"):
            self.save_m2m()
    return user
```
核心语义：**commit 时不仅要 `user.save()`，还要 `self.save_m2m()` 保存 M2M 关系**。`super().save(commit=False)` 延迟保存并在 form 上挂了 `save_m2m` 方法（仅当有 M2M 字段时存在，故用 `hasattr` 守卫）。`set_password` 后须 `user.save()` 落库主对象，再 `save_m2m()` 落库多对多。漏掉 `save_m2m` 则 M2M 数据丢失。三要素：(1) `commit` 为真时执行；(2) `hasattr` 正确探测 `save_m2m`；(3) 在正确对象（self，即 form）上调用。

F2P 测试 `UserCreationFormTest.test_custom_form_saves_many_to_many_field`：自定义含 `orgs`（M2M）的 UserCreationForm，`save(commit=True)` 后断言 `user.orgs.all() == [organization]`。

## 调用链分析

`form.save(commit=True)` → `super().save(commit=False)` 返回未落库的 user 并在 form 上设置 `self.save_m2m`（ModelForm 机制：commit=False 时延迟 M2M 保存，提供 save_m2m 供后续调用）→ `user.save()` 落库 user → `self.save_m2m()` 落库 M2M。M2M 字段（如 `orgs`）的值靠 `save_m2m` 写入中间表。漏调、条件错、对象错都会让 M2M 不保存。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `if commit`→`if not commit`，提交逻辑反转 |
| B | 🔴 必须替换 | 替换 | 原 B 与 A 字节相同；改为 `hasattr`→`not hasattr`，永不调 save_m2m |
| C | 🔴 必须替换 | 替换 | 原 C 与 A 字节相同；改为在错误对象（user）上调 save_m2m |
| D | 🟢 高质量 | 保留 | 删除 save_m2m 调用，还原原 bug |
| E | 🟢 高质量 | 保留 | save_m2m 藏到默认关闭的参数开关后 |

原 A、B、C 字节完全相同（`if commit`→`if not commit`）。保留 A，重做 B、C；D、E 已各异保留。

## 各组 Mutation 分析

### Group A — 保留（B3 条件反转）
```diff
         user.set_password(self.cleaned_data["password1"])
-        if commit:
+        if not commit:
             user.save()
             if hasattr(self, "save_m2m"):
                 self.save_m2m()
```
**变异语义**：提交条件反转。`commit=True`（F2P 场景）时不进入分支 → user 和 M2M 都不保存（user 甚至没落库）；`commit=False` 时反而保存。整个保存语义颠倒。保留。

### Group B — 替换（B3 条件反转：hasattr）
**原**：与 A 字节相同（`if commit`→`if not commit`）。
**最终 mutation**：
```diff
             user.save()
-            if hasattr(self, "save_m2m"):
+            if not hasattr(self, "save_m2m"):
                 self.save_m2m()
```
**变异语义**：`save_m2m` 探测条件反转——只在 form **没有** `save_m2m` 方法时才调它。有 M2M 字段时 `save_m2m` 存在 → `not hasattr` 为 False → 不调用，M2M 不保存（F2P 失败）；理论上没有 save_m2m 时进分支调一个不存在的方法会 AttributeError，但那种情况测试不覆盖。user.save() 仍正常（与 A 不同，A 连 user 都不存）。模拟"hasattr 守卫写反"。

### Group C — 替换（D1 状态：错误接收对象）
**原**：与 A 字节相同（`if commit`→`if not commit`）。
**最终 mutation**：
```diff
             user.save()
-            if hasattr(self, "save_m2m"):
-                self.save_m2m()
+            if hasattr(user, "save_m2m"):
+                user.save_m2m()
```
**变异语义**：在 `user`（模型实例）而非 `self`（form）上探测并调用 `save_m2m`。`save_m2m` 是 **form** 的方法，model 实例没有 → `hasattr(user, "save_m2m")` 为 False → 整个分支跳过，M2M 不保存。代码结构看似正确（hasattr 守卫 + 调用都在），但接收对象错了。模拟"self/user 接收者混淆"。比删除调用隐蔽——调用还在、只是对象错。

### Group D — 保留（B2 删除调用）
```diff
             user.save()
-            if hasattr(self, "save_m2m"):
-                self.save_m2m()
         return user
```
**变异语义**：删除 `save_m2m` 调用，commit 时只保存 user、不保存 M2M，还原原 bug。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
-    def save(self, commit=True):
+    def save(self, commit=True, save_m2m_fields=False):
...
             if hasattr(self, "save_m2m"):
-                self.save_m2m()
+                if save_m2m_fields:
+                    self.save_m2m()
```
**变异语义**：新增参数 `save_m2m_fields`（默认 False），`save_m2m` 只在显式传 True 时才调。默认调用 `save(commit=True)` 不传该参数 → 不保存 M2M。模拟"把 M2M 保存做成可选、默认却关掉"。保留。

## 新设计 Mutation 说明

原 A、B、C 字节完全相同（`if commit`→`if not commit`）。本次保留 A（提交条件反转）、D（删调用）、E（默认关闭参数开关），把与 A 重复的 B 重做为"`hasattr`→`not hasattr`（探测条件反转，永不调 save_m2m 但 user 仍保存）"、C 重做为"在 user 而非 self 上调 save_m2m（接收对象错误）"。五组覆盖"提交条件反转 / hasattr 反转 / 错误接收对象 / 删调用 / 默认关闭开关"五个角度。全部实测：golden 通过、五个变异均令 F2P（`test_custom_form_saves_many_to_many_field`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
