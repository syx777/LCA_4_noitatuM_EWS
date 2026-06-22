# django__django-14534 Mutation 分析

## 问题背景

`BoundWidget.id_for_label` 在渲染 `CheckboxSelectMultiple` 等带 choices 的 widget 的子控件 label 时，
硬编码生成 ID 字符串 `'id_%s_%s' % (name, index)`，完全忽略了 `ChoiceWidget.options` 已经计算好并存放在
`self.data['attrs']['id']` 里的真实 ID。当用户在初始化 Form 时通过 `auto_id='prefix_%s'` 自定义 ID 前缀时，
`subwidgets[i].id_for_label` 仍然返回 `id_field_0` 而不是 `prefix_field_0`，导致 `<label for=...>` 指向错误。

## Golden Patch 语义分析

```python
@property
def id_for_label(self):
-    return 'id_%s_%s' % (self.data['name'], self.data['index'])
+    return self.data['attrs'].get('id')
```

核心语义有两点：
1. **数据来源切换**：不再用 `name`+`index` 重新拼字符串，而是直接读取 widget options 已经算好的 `attrs['id']`，
   从而正确反映 `auto_id` 前缀。
2. **缺省处理**：使用 `.get('id')` 而非 `['id']`，当 `attrs` 中没有 `id`（如 `auto_id=False` 的 Select）时返回 `None`
   而不是抛 `KeyError`。

## 调用链分析

`BoundField.subwidgets` → `self.field.widget.subwidgets(html_name, value, attrs={'id': id_})` 生成 options dict 列表
（每个 dict 含 `attrs={'id': 'prefix_field_0'}`）→ 包装为 `BoundWidget(data=dict)` → 模板/测试调用
`BoundWidget.id_for_label` 读取 `data['attrs']['id']`。

F2P 测试：
- `test_boundfield_subwidget_id_for_label`：`auto_id='prefix_%s'` 时期望 `subwidgets[0].id_for_label == 'prefix_field_0'`。
- `test_iterable_boundfield_select`：`auto_id=False` 的 Select，期望 `fields[0].id_for_label == None`（attrs 为空 dict）。

两个测试覆盖两条正交路径：有 id 的前缀路径 与 无 id 的缺省路径。

## 替换决策总览

| 槽位 | 原 strategy_group | 原始内容 | 分类 | 决策 | 新 strategy_code |
|------|------|----------|------|------|------|
| A | A | 直接还原 golden（拼回 `'id_%s_%s'`） | 🔴 golden-revert 冗余 | 替换 | A1 |
| B | B | `self.data['attrs']['id']`（KeyError 语义） | 🟢 独特异常失败模式 | 保留 | B2 |
| D | D | 与 B 字节完全相同的重复 diff | 🔴 重复 diff | 替换 | D3 |
| E | E | 带 `# E2:` 注释 + 伪造 `custom_id_format` 属性 | 🔴 非自然人造痕迹 | 替换 | C3 |

🟡 浅层单 token 变异计数 N：原始 4 个中仅 B 属于自然单 token（保留），其余为冗余/人造，按规则全部替换。

## 各组 Mutation 分析

### 槽 A（替换）
原始 diff 把代码逐字还原成 golden 修复前的 `'id_%s_%s' % (name, index)`，是最典型的 golden-revert 冗余，
LLM 测试生成器一眼可辨，必须替换。

### 槽 B（保留）
`self.data['attrs']['id']` 仅去掉 `.get` 的兜底，是自然的单 token 改动。它在「有 id」路径返回正确值，
但在 `auto_id=False` 的 Select 路径（attrs 为空 dict）抛 `KeyError`，构成**异常类型**维度的独特失败模式
（实测：`test_iterable_boundfield_select` 报 `KeyError: 'id'`）。与其它槽失败模式正交，保留。

### 槽 D（替换）
与槽 B 字节完全一致的重复 diff，按规则必须替换。

### 槽 E（替换）
含 `# E2:` 注释并引用根本不存在的 `parent_widget.custom_id_format` 属性，分支逻辑诡异，是明显的人造痕迹，必须替换。

## 新设计 Mutation 说明

为最大化失败模式多样性，三个替换互相正交，且与保留的槽 B（KeyError）互不重叠：

- **槽 A → A1（缺省语义篡改）**：`get('id', '')`。把缺省值从 `None` 改成空串 `''`。
  只在「无 id」路径（`auto_id=False` Select）暴露：`'' != None`，专门击穿 `test_iterable_boundfield_select`。
  极隐蔽——给 `.get` 加默认值看起来像无害的防御式编程。

- **槽 D → D3（数据来源依赖错误）**：`self.parent_widget.attrs.get('id')`。
  从 `data['attrs']`（每个 option 自己的 id）误读为 `parent_widget.attrs`（整个 widget 级 attrs，通常为空 `{}`）。
  在前缀路径返回 `None`，击穿 `test_boundfield_subwidget_id_for_label`。属于「读错对象层级」的顺序依赖错误，
  与槽 A/B 的失败测试不同。

- **槽 E → C3（文本拼接污染）**：`'%s_%s' % (self.data['attrs'].get('id'), self.data['index'])`。
  在正确 id 后又追加 `_index`，看起来像保留 index 后缀的「兼容」写法。前缀路径得到 `prefix_field_0_0`，
  缺省路径得到 `'None_0'`，两个 F2P 测试都失败（failures=2）。属于文本/格式编码维度的污染。
