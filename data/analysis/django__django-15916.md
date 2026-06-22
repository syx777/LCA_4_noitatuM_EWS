# django__django-15916

## 问题背景

`ModelForm` 的 `Meta` 无法指定 `formfield_callback`，且 `modelform_factory(model, form=BaseForm)` 在未显式传 callback 时不会继承基类 form 的 `formfield_callback`，而是用 None 覆盖。Golden patch 重构：把 `formfield_callback` 纳入 `ModelFormOptions`（从 `Meta` 读取），`ModelFormMetaclass.__new__` 不再手动从 bases 收集 callback、改用 `opts.formfield_callback`，并让 `modelform_factory` 不再把 callback 塞进顶层 attrs（避免覆盖 Meta 的值）。

## Golden Patch 语义分析

```python
class ModelFormOptions:
    def __init__(self, options=None):
        ...
        self.formfield_callback = getattr(options, "formfield_callback", None)

class ModelFormMetaclass(DeclarativeFieldsMetaclass):
    def __new__(mcs, name, bases, attrs):
        new_class = super().__new__(mcs, name, bases, attrs)
        ...
        fields = fields_for_model(
            opts.model, opts.fields, opts.exclude, opts.widgets,
            opts.formfield_callback,   # 原为局部变量 formfield_callback
            ...
        )

def modelform_factory(...):
    ...
    Meta = type("Meta", bases, attrs)
    if formfield_callback:
        Meta.formfield_callback = staticmethod(formfield_callback)
    ...
    form_class_attrs = {"Meta": Meta}   # 原含 "formfield_callback": formfield_callback
```
核心语义：**`formfield_callback` 成为 `Meta`（`ModelFormOptions`）的一等属性**。元类构造字段时从 `opts.formfield_callback` 取值（它由 `Meta` 经 `ModelFormOptions` 读入，支持继承）；`modelform_factory` 只在显式传 callback 时写到 `Meta`，否则让其从基类 `Meta` 继承，不再用顶层 attrs 的 None 覆盖。三处协同：Options 读入、元类消费、factory 不再覆盖。

F2P 测试两个：`FormFieldCallbackTests.test_custom_callback_in_meta`（`Meta.formfield_callback` 生效，所有字段用 Textarea）与 `test_custom_callback_from_base_form_meta`（`modelform_factory` 继承基类 Meta 的 callback）。

## 调用链分析

定义 `ModelForm` 子类时触发 `ModelFormMetaclass.__new__`：`opts = ModelFormOptions(Meta)` 读入含 `formfield_callback` 的所有 Meta 选项 → `fields_for_model(..., opts.formfield_callback, ...)` 用该 callback 为每个 model 字段生成 formfield。`modelform_factory` 动态造 `Meta` 类：若显式传 callback 则 `Meta.formfield_callback = staticmethod(callback)`，否则继承 `form.Meta`（bases）的 callback；`form_class_attrs` 不再带顶层 `formfield_callback`，避免它覆盖 Meta 继承链。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 补充 | 新增 | 原缺 A 组；`ModelFormOptions` 读错属性名（`field_classes`） |
| B | 🟢 高质量 | 保留 | 元类传 `None` 而非 `opts.formfield_callback`，callback 不生效 |
| C | 🟢 高质量 | 保留 | factory 中 `if formfield_callback`→`if not`，反转写入条件 |
| D | 🟢 高质量 | 保留 | factory 在未传 callback 时强行写 `attrs["formfield_callback"]=None` 覆盖继承 |
| E | 🟢 高质量 | 保留 | 用默认关闭参数 `inherit_formfield_callback` 把继承禁掉 |

原实例只有 B/C/D/E 四组，缺 A。补充 A，B/C/D/E 各为不同机制故保留。

## 各组 Mutation 分析

### Group A — 补充（A1 接口契约：读错 Meta 属性名）
```diff
-        self.formfield_callback = getattr(options, "formfield_callback", None)
+        self.formfield_callback = getattr(options, "field_classes", None)
```
**变异语义**：`ModelFormOptions.__init__` 把 `formfield_callback` 从 `Meta` 的 `field_classes` 属性读取（相邻选项名）。`field_classes` 通常为 None 或一个 dict，绝非可调用的 callback。于是 `opts.formfield_callback` 拿到错误值（None 或 dict），元类用它构造字段时 callback 不生效（或若是 dict 则在调用处出错）。`Meta` 里真正的 `formfield_callback` 被忽略。模拟"复制粘贴相邻 getattr 行、属性名没改对"。F2P 失败。

### Group B — 保留（D1 状态：传 None 而非 opts 值）
```diff
                 opts.widgets,
-                opts.formfield_callback,
+                None,
```
**变异语义**：元类调 `fields_for_model` 时 callback 参数硬传 `None`，无视 `opts.formfield_callback`。所有字段用默认 formfield，Meta 指定的 callback 完全失效。`test_custom_callback_in_meta` 断言字段用 Textarea 失败。保留。

### Group C — 保留（B3 条件反转）
```diff
-    if formfield_callback:
+    if not formfield_callback:
         Meta.formfield_callback = staticmethod(formfield_callback)
```
**变异语义**：`modelform_factory` 写 `Meta.formfield_callback` 的条件反转。显式传了 callback 时（truthy）`not` 为 False → **不**写入 → Meta 用不到它；未传时（None）反而写入 `staticmethod(None)`。逻辑颠倒，两个 F2P 都受影响。保留。

### Group D — 保留（D1 状态：强行覆盖继承）
```diff
     if field_classes is not None:
         attrs["field_classes"] = field_classes
+
+    # Only set formfield_callback if explicitly provided
+    if formfield_callback is None:
+        attrs["formfield_callback"] = None
```
**变异语义**：当未显式传 callback 时，往顶层 `attrs` 写 `formfield_callback = None`。该 attrs 用于造 `Meta` 类，None 会覆盖从基类 `form.Meta` 继承来的 callback。`test_custom_callback_from_base_form_meta`（依赖继承）失败。注释说"仅在显式提供时设置"，实则做了相反的事（未提供时反而设了 None）。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
     field_classes=None,
+    inherit_formfield_callback=False,
 ):
...
     if formfield_callback:
         Meta.formfield_callback = staticmethod(formfield_callback)
+    elif not inherit_formfield_callback:
+        # Explicitly set formfield_callback to None to prevent inheritance
+        Meta.formfield_callback = None
```
**变异语义**：新增参数 `inherit_formfield_callback`（默认 False）。未传 callback 时，因默认不继承 → `Meta.formfield_callback = None`，覆盖基类继承。只有显式传 `inherit_formfield_callback=True` 才保留继承。把"继承基类 callback"这一修复点做成默认关闭的开关。`test_custom_callback_from_base_form_meta` 失败。保留。

## 新设计 Mutation 说明

原实例只有 B/C/D/E 四组，缺 A。本次保留 B（元类传 None）、C（factory 写入条件反转）、D（强行写 None 覆盖继承）、E（默认关闭的 inherit 开关），补充 A——在数据源头 `ModelFormOptions.__init__` 把 `formfield_callback` 从 `Meta` 的相邻属性 `field_classes` 读取（属性名错配）。五组分布在 `ModelFormOptions`（A）、`ModelFormMetaclass`（B）、`modelform_factory`（C/D/E）三处、覆盖"读错属性名 / 传 None / 条件反转 / 覆盖继承 / 默认关闭开关"五个角度。全部实测：golden 通过、五个变异均令两个 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
