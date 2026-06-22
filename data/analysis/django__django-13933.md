# django__django-13933

## 问题背景

`ModelChoiceField` 在校验失败抛出 `ValidationError` 时，不像 `ChoiceField` 那样把无效的输入值带进错误信息。这导致用户即使把 `invalid_choice` 错误消息自定义成包含 `%(value)s` 占位符，也无法显示到底是哪个值非法。Golden patch 在 `to_python` 抛错时给 `ValidationError` 传入 `params={'value': value}`，使占位符 `%(value)s` 能被无效值替换。

## Golden Patch 语义分析

```python
def to_python(self, value):
    if value in self.empty_values:
        return None
    try:
        key = self.to_field_name or 'pk'
        if isinstance(value, self.queryset.model):
            value = getattr(value, key)
        value = self.queryset.get(**{key: value})
    except (ValueError, TypeError, self.queryset.model.DoesNotExist):
        raise ValidationError(
            self.error_messages['invalid_choice'],
            code='invalid_choice',
            params={'value': value},
        )
    return value
```

核心语义：当按 `key`（默认 `pk`）查询 queryset 找不到对应对象、或值类型不合法时，抛出 `invalid_choice` 错误。修复点是把当前的无效 `value` 作为 `params['value']` 传入——`ValidationError` 在渲染时会用 `params` 对消息做 `%` 格式化，于是自定义消息里的 `%(value)s` 能正确替换成那个非法输入。

F2P 测试 `test_modelchoicefield_value_placeholder` 自定义消息为 `'"%(value)s" is not one of the available choices.'`，对 `'invalid'` 输入断言渲染结果为 `'"invalid" is not one of the available choices.'`。任何让 `params['value']` 缺失、为错误内容、或在该场景下变空的变异都会使断言失败。

## 调用链分析

- `to_python` 被 `Field.clean` → `ModelChoiceField` 校验流程调用；测试通过 `f.clean('invalid')` 触发。
- 数据流：`value='invalid'` 是普通字符串（非模型实例），跳过 `getattr` 分支；`self.queryset.get(pk='invalid')` 因 pk 不存在/类型不符抛 `ValueError`/`DoesNotExist`，进入 `except`。此时 `value` 仍是原始输入 `'invalid'`，`key='pk'`。
- `ValidationError(msg, code, params)` 把 `params` 存入异常；最终 `Field` 收集错误并用 `message % params` 渲染。占位符匹配依赖 `params` 字典中存在键 `'value'` 且其值为期望内容。
- 关键变量区分：`value`（无效输入，应被展示）vs `key`（查询字段名 `'pk'`）；`self.required`（字段是否必填，默认 `True`）；`self.to_field_name`（默认 `None`）。多个 mutation 利用这些相邻变量/配置做混淆。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原 mutation 删除 `params={'value': value}`，是 golden patch 的字节级逆操作，直接冗余 |
| B | 🔴 必须替换 | 替换 | 同 A，字节级直接还原 |
| C | 🔴 必须替换 | 替换 | 改默认消息为 `%(value)s` 但把 params 清空，效果等同还原（占位符无值），功能等价冗余 |
| D | 🔴 必须替换 | 替换 | 同 A，字节级直接还原 |

> 本实例 mutations.jsonl 只有 4 个 mutation（无 E 组）。A/B/D 三份完全相同（删除 params 行），C 是把 params 置空的功能等价还原。4 个全部 🔴 必须替换。

语义浅层共 0 个；必须替换 4 个，全部替换为高质量变异。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：删除 `params={'value': value},`（golden patch 逆操作）
**分类**：🔴 必须替换（直接冗余）

**最终 mutation**：
```diff
-                params={'value': value},
+                params={'value': key},
```
**变异语义**：把传入占位符的变量从 `value`（无效输入）改成 `key`（查询字段名，默认 `'pk'`）。两个变量在同一行上下文里紧邻、都是局部变量，看起来像"传哪个都行"的笔误。但语义完全错位：`%(value)s` 会被渲染成 `'pk'` 而非用户输入的 `'invalid'`，错误信息变得毫无意义。由于 params 仍然非空、键名 `'value'` 也正确，只是值取错，审查者若不追踪 `value`/`key` 各自的含义很难发现。属 A1（修改参数语义：传错了内容）。F2P 失败（渲染出 `'"pk" is not...'`）。

### Group B — 替换
**原 mutation**：删除 `params={'value': value},`（同 A）
**分类**：🔴 必须替换（直接冗余）

**最终 mutation**：
```diff
+            params = {'value': value} if isinstance(value, self.queryset.model) else {}
             raise ValidationError(
                 self.error_messages['invalid_choice'],
                 code='invalid_choice',
-                params={'value': value},
+                params=params,
             )
```
**变异语义**：把 params 的构造改成"仅当 `value` 是模型实例时才带上，否则为空"。看似一个合理的防御：`value` 可能是模型对象也可能是原始输入，按类型决定是否注入。但实际进入 `except` 分支时，普通表单提交的 `value` 几乎总是原始字符串/整数（非模型实例），所以 params 恒为 `{}`，占位符永远拿不到值。这是对 None/类型边界的错误处理——只覆盖了罕见的"传模型实例"路径，遗漏了主路径。属 B2（移除/错误处理空与类型的边界情况）。F2P 失败（`'invalid'` 非模型实例 → params 空）。

### Group C — 替换
**原 mutation**：改默认消息为 `%(value)s` 同时 `params={}`（功能等价还原）
**分类**：🔴 必须替换（功能等价冗余）

**最终 mutation**：
```diff
-                params={'value': value},
+                params={'value': value} if self.to_field_name else {},
```
**变异语义**：让 params 只在设置了 `to_field_name` 时才注入。`to_field_name` 默认为 `None`（绝大多数 `ModelChoiceField` 不指定，按 pk 查询），所以默认情况下 params 为空。表面像是"只有按特定字段查时才需要展示值"的合理优化，但实际上无论按 pk 还是按字段查，错误信息都应包含无效值。这是用一个看似相关、实则不相干的配置项去门控本应无条件执行的逻辑。属 C1（破坏隐式行为：依赖了不该依赖的配置维度）。F2P 失败（测试未设 `to_field_name` → params 空）。

### Group D — 替换
**原 mutation**：删除 `params={'value': value},`（同 A）
**分类**：🔴 必须替换（直接冗余）

**最终 mutation**：
```diff
-                params={'value': value},
+                params={'value': value} if not self.required else {},
```
**变异语义**：把 params 注入门控在 `not self.required` 上——即只有可选字段才带无效值，必填字段不带。`self.required` 默认 `True`，所以默认（含 F2P 测试）的必填字段走空 params 分支。看起来像是"必填字段的错误信息更简洁"的产品取舍，但 `required` 与"是否应展示无效值"在语义上毫无关系，是把环境/配置状态错误地耦合进了消息构造。属 D1（按对象配置状态做不当的条件初始化，破坏多数场景）。F2P 失败（默认 `required=True` → params 空）。

## 新设计 Mutation 说明

四个变异都落在 `to_python` 的 `params` 构造上，但分布在四个互不重叠的失效维度：
- **A**：params **值取错**（`value`→`key`），params 仍非空，最隐蔽。
- **B**：按 **value 类型** 门控（只在模型实例时注入），漏掉字符串主路径。
- **C**：按 **to_field_name 配置** 门控（默认 None → 空）。
- **D**：按 **required 状态** 门控（默认 True → 空）。

B/C/D 都伪装成"合理的条件化注入"，但各自绑定了一个不相关的判据，使默认/测试场景恰好落入空 params 分支；A 则是纯粹的变量混淆。全部仅修改 `django/forms/models.py`（允许文件），不触碰测试。均通过 Step 5 实证自查：base_commit → golden patch → test_patch 后可干净应用、`py_compile` 通过，并实际运行 `ModelChoiceFieldErrorMessagesTestCase`（2 个测试）确认每个变异都使 F2P 测试 `test_modelchoicefield_value_placeholder` 失败，同时不破坏 `test_modelchoicefield`。
