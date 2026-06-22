# django__django-16139

## 问题背景

通过另一个模型的 admin（其外键设了 `to_field="uuid"` 等非 pk 字段）访问 `UserAdmin` 时，URL 形如 `.../user/<uuid>/change/?_to_field=uuid`，而 `UserChangeForm` 里 password 帮助文本的链接被硬编码成相对路径 `"../password/"`，基于"UserAdmin 总是用 pk 访问"的假设，导致改密码链接 404。Golden patch 把相对链接改成 `f"../../{self.instance.pk}/password/"`——显式用实例的 pk 构造绝对到 pk 的路径，不再依赖当前 URL 用的是 pk 还是 to_field。

## Golden Patch 语义分析

```python
password = self.fields.get("password")
if password:
    password.help_text = password.help_text.format(
        f"../../{self.instance.pk}/password/"
    )
```
核心语义：**改密链接必须基于实例的真实 pk 构造，而非假设当前 URL 段就是 pk**。原 `"../password/"` 是相对当前 `<id>/change/` 的，当 `<id>` 是 to_field 值（如 uuid）时，相对路径解析错误。新写法 `f"../../{self.instance.pk}/password/"` 先上退两级（出 `<id>/change/`），再用真实 pk 进入 `<pk>/password/`，无论当前 URL 用 pk 还是 to_field 都正确。要点：(1) 上退层级 `../../`（两级）；(2) 用 `self.instance.pk`（真实主键）；(3) 拼成 `<pk>/password/`。

F2P 测试 `UserChangeFormTest.test_link_to_password_reset_in_helptext_via_to_field`：经 to_field 的 admin URL 构造 UserChangeForm，解析 help_text 里的链接，断言 `urljoin(change_url, link)` 等于真实的 `<pk>/password/` 改密 URL。

## 调用链分析

`UserChangeForm.__init__` 取 password 字段，把其 `help_text`（含 `{}` 占位）`.format(链接)`。链接是相对 admin change 页的路径。admin change URL 可能是 `/user/<pk>/change/` 或 `/user/<to_field_value>/change/`。测试用 `urljoin(admin_change_url, link)` 解析相对链接，期望落到 `/user/<pk>/password/`。`../../{pk}/password/` 从 `/user/<id>/change/` 上退到 `/user/`，再进 `<pk>/password/`。层级数、用的字段（pk vs username/instance）、路径字面任一错都会让链接指向错误位置。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `../../`→`../`，上退层级少一级，链接解析错位 |
| B | ➕ 补充 | 新增 | 原缺 B；`self.instance.pk`→`self.instance.username`，用错字段 |
| C | 🟢 高质量 | 保留 | 还原 `"../../password/"`（丢 pk 段），漏了实例标识 |
| D | ➕ 补充 | 新增 | 原缺 D；`self.instance.pk`→`self.instance`，用整个对象而非 pk |
| E | ➕ 补充 | 新增 | 原缺 E；正确路径藏到默认关闭开关后，否则回退旧 `../password/` |

原实例只有 A、C 两组，缺 B、D、E。补充 B、D、E，保留 A、C。

## 各组 Mutation 分析

### Group A — 保留（B3 边界：层级少一级）
```diff
             password.help_text = password.help_text.format(
-                f"../../{self.instance.pk}/password/"
+                f"../{self.instance.pk}/password/"
             )
```
**变异语义**：上退层级从 `../../`（两级）变成 `../`（一级）。从 `/user/<id>/change/` 只退一级到 `/user/<id>/`，再进 `<pk>/password/` → 得 `/user/<id>/<pk>/password/`，多了一段 `<id>`，链接错误。模拟"相对路径少算一级"的经典 off-by-one。保留。

### Group B — 补充（C1 值/数据形状：用错字段）
```diff
-                f"../../{self.instance.pk}/password/"
+                f"../../{self.instance.username}/password/"
```
**变异语义**：用 `self.instance.username` 而非 `self.instance.pk` 构造链接。改密 URL 路由按 pk 匹配，用 username 拼出的 `/user/<username>/password/` 与期望的 `/user/<pk>/password/` 不符（除非 username 恰等于 pk）。看起来"用了实例的某个标识字段"，实则该路由要的是 pk。模拟"拿错了实例的标识属性"。

### Group C — 保留（D1 状态：丢失实例标识段）
```diff
-            password.help_text = password.help_text.format(
-                f"../../{self.instance.pk}/password/"
-            )
+            password.help_text = password.help_text.format("../../password/")
```
**变异语义**：链接退化成 `"../../password/"`，完全丢掉 `{self.instance.pk}` 段。从 `/user/<id>/change/` 退两级到 `/user/`，再进 `password/` → `/user/password/`，缺少 pk 段，指向错误位置。比原 bug（`../password/`）更早一级，但同样漏了实例标识。保留。

### Group D — 补充（C1 类型/数据形状：用整个对象而非 pk）
```diff
-                f"../../{self.instance.pk}/password/"
+                f"../../{self.instance}/password/"
```
**变异语义**：f-string 插入 `self.instance`（整个 model 对象）而非 `self.instance.pk`。`str(user_instance)` 是 User 的 `__str__`（通常是 username 或其它），不是 pk，拼出的路径段是对象的字符串表示而非主键值，链接错误。模拟"忘了 `.pk`、直接插了对象"。与 B（用 username 属性）不同——D 是用对象本身、依赖 `__str__`，更隐蔽。

### Group E — 补充（E2 隐式→显式开关）
```diff
-            password.help_text = password.help_text.format(
-                f"../../{self.instance.pk}/password/"
-            )
+            if getattr(self, "use_pk_in_password_url", False):
+                password.help_text = password.help_text.format(
+                    f"../../{self.instance.pk}/password/"
+                )
+            else:
+                password.help_text = password.help_text.format("../password/")
```
**变异语义**：正确的 pk 链接藏到实例属性开关 `use_pk_in_password_url` 后，默认 `False` → 走 else 用旧 `"../password/"`（原 bug）。只有显式设 True 才用 pk 绝对路径。模拟"把修复做成可配置、默认却保留旧行为"。

## 新设计 Mutation 说明

原实例只有 A、C 两组（A 改层级、C 丢 pk 段），缺 B、D、E。本次保留 A（层级 off-by-one）、C（丢实例标识段），补充 B（`pk`→`username` 用错属性）、D（`pk`→整个 instance 对象、依赖 `__str__`）、E（正确 pk 链接藏到默认关闭的 `use_pk_in_password_url` 开关后，否则回退旧 `../password/`）。五组覆盖"层级 off-by-one / 错误属性 / 丢 pk 段 / 用整个对象 / 默认关闭开关"五个角度，B 与 D 虽都"用错标识"但机制不同（属性名 vs 对象 `__str__`）。全部实测：golden 通过、五个变异均令 F2P（`test_link_to_password_reset_in_helptext_via_to_field`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
