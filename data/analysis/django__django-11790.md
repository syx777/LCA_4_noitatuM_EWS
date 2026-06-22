# django__django-11790 Mutation 治理分析

## 问题背景

`AuthenticationForm` 的 `username` 字段不再渲染 `maxlength` HTML 属性（在 #27515 / commit `5ceaf14` 引入的回归）。
此前 `username` 字段的 `max_length` 会同时设置在表单字段（用于校验）和 widget 的 `attrs['maxlength']`（用于前端渲染）。
回归后只设置了字段的 `max_length`，widget 的 `maxlength` 丢失，导致 HTML 不再带 `maxlength`。

## Golden Patch 语义分析

```python
# before (buggy)
self.fields['username'].max_length = self.username_field.max_length or 254
# after (golden)
username_max_length = self.username_field.max_length or 254
self.fields['username'].max_length = username_max_length
self.fields['username'].widget.attrs['maxlength'] = username_max_length
```

核心语义：把 `max_length or 254` 抽成局部变量 `username_max_length`，
并**同时**赋给字段校验长度与 widget 的 `maxlength` 属性。
`or 254` 的语义是：当 `USERNAME_FIELD` 的 `max_length` 为 `None`（如 `IntegerUsernameUser`）时回退为默认 254；
当字段有显式长度（如 `CustomEmailField` = 255）时使用该长度。

## 调用链分析

- `AuthenticationForm.__init__` → 读取 `UserModel._meta.get_field(USERNAME_FIELD).max_length`。
- 数据流：`username_field.max_length`（可能 `None`）→ `or 254` 回退 → 同时写入字段 `max_length` 与 widget `attrs['maxlength']`。
- F2P 测试直接断言 `form.fields['username'].widget.attrs.get('maxlength')`：
  - `test_username_field_max_length_matches_user_model`：`CustomEmailField` → 期望 255。
  - `test_username_field_max_length_defaults_to_254`：`IntegerUsernameUser`（`max_length=None`）→ 期望 254。
- 两个测试分别覆盖「显式长度」与「None 默认回退」两条分支，构成正交的失败检测点。

## 替换决策总览

| 组 | 原策略 | 原 diff 概述 | 分类 | 决策 | 最终策略码 |
|---|---|---|---|---|---|
| A | A | 注释掉 golden 新增的 widget maxlength 行 | 🔴 直接还原 golden | 替换 | A1 |
| B | B | `or 254` → `and 254` | 🟡 浅层（B3，M=1→floor(1/2)=0） | 保留 | B3 |
| C | C | widget maxlength 改为 `str(...) or '254'` | 🟢 类型契约变异 | 保留 | C1 |
| E | E | 用未定义变量 `use_maxlength` 包裹赋值 | 🔴 NameError，破坏所有实例化（含 P2P） | 替换 | D3 |

共 4 行（少于 5，按实际组工作，不臆造）。替换 2 个（A、E），保留 2 个（B、C）。

## 各组 Mutation 分析

### A 组（替换）

- 原 diff：将 golden 新增的 `self.fields['username'].widget.attrs['maxlength'] = username_max_length` 直接注释。
- 分类：🔴 必须替换。这是 golden patch 的逆操作（直接还原 buggy 状态），属于「直接冗余」。
- 替换设计（A1，参数语义变异）：

```python
-        self.fields['username'].widget.attrs['maxlength'] = username_max_length
+        self.fields['username'].widget.attrs['maxlength'] = self.username_field.max_length or 150
```

- 变异语义：字段校验仍用 `or 254`，但 widget 的 maxlength 改用 `or 150` 的不同回退值，并绕过局部变量重新计算。
  对有显式长度的模型（255）两条路径一致，`matches_user_model` 通过；
  仅在 `max_length is None` 的默认分支上 widget 得到 150 而非 254，使 `defaults_to_254` 失败。
  看起来像是对原 `or 254` 的复制粘贴笔误，审查极易忽略。

### B 组（保留）

- 原 diff：`username_max_length = self.username_field.max_length or 254` → `... and 254`。
- 分类：🟡 语义浅层（B3 布尔逻辑反转，单 token）。本组浅层数 M=1，floor(1/2)=0，不替换。
- 保留理由：修改位于关键回退逻辑节点，同时污染字段 `max_length` 与 widget `maxlength`，
  对有长度模型得 254（应为 255），对 None 得 None，能被两个 F2P 同时捕获，但单字符改动自然且难审查。
- 变异语义：把「缺省回退」误写成「短路截断」。

### C 组（保留）

- 原 diff：`self.fields['username'].widget.attrs['maxlength'] = str(self.username_field.max_length) or '254'`。
- 分类：🟢 类型契约变异（C1，破坏隐式类型协约）。保留。
- 保留理由：把 maxlength 存成 `str`，且重新从原始字段长度推导、丢掉 254 回退（`str(None)` 是真值 `'None'`）。
  对 int 的数值相等断言会失败，但任何只检查存在性或渲染 HTML（int 本会被字符串化）的测试仍能通过，类型回归隐蔽。
- 变异语义：返回值类型从 int 变 str，并改变 None 的回退行为。

### E 组（替换）

- 原 diff：`if use_maxlength:` 包裹赋值，但 `use_maxlength` 未定义。
- 分类：🔴 必须替换。`use_maxlength` 未定义会在**每次** `AuthenticationForm` 实例化时抛 `NameError`，
  破坏整个测试模块（含大量 P2P），属于不自然且过度破坏的人工痕迹。
- 替换设计（D3，顺序/前置条件依赖变异）：

```python
-        self.fields['username'].widget.attrs['maxlength'] = username_max_length
+        if username_max_length <= 254:
+            self.fields['username'].widget.attrs['maxlength'] = username_max_length
```

- 变异语义：给 widget maxlength 赋值加上一个隐藏的上界前置条件 `<= 254`。
  长度 ≤ 254 的模型（默认分支）行为正确，`defaults_to_254` 通过；
  仅当 `USERNAME_FIELD` 长度更大（255）时 widget 静默丢失 maxlength，使 `matches_user_model` 失败。
  看起来像一处「防御性边界检查」而非缺陷。

## 新设计 Mutation 说明与正交性

- A1 在「None 默认分支」失败（widget=150≠254）。
- D3（E 组）在「显式长度分支」失败（widget=None≠255）。
- 二者失败方向相反、覆盖不同 F2P，与保留的 B（两分支同时失败）、C（类型不匹配）构成 4 个正交失败模式。

## 验证结果（REAL）

环境：将 base 仓库 `cp` 到 `/tmp/ws_11790`，依次 `patch -p1` 应用 golden + test_patch，`git commit` 为 golden 基线。
F2P 标识：
- `auth_tests.test_forms.AuthenticationFormTest.test_username_field_max_length_matches_user_model`
- `auth_tests.test_forms.AuthenticationFormTest.test_username_field_max_length_defaults_to_254`

- 基线（无变异）：F2P 全部 PASS；整模块 `auth_tests.test_forms` 79 tests OK。
- 每个最终 mutation（A1 / B3 / C1 / D3）：
  - 嵌入 JSONL 的 diff 均能 `patch -p1` 干净应用，`py_compile` 通过。
  - F2P 失败（A1、D3 各失败 1 个对应分支；B3、C1 失败 2 个）。
  - 整模块单进程运行：仅 F2P 相关用例失败，无任何 P2P 回归（A1/D3 仅 1 failure，B3/C1 仅 2 failures，其余全过）。

均通过真实运行验证。
