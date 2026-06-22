# django__django-15103

## 问题背景

`json_script` 过滤器/工具函数原本强制要求 `element_id` 参数（生成 `<script id="...">`）。用户希望在 `<template>` 内使用时无需 id。Golden patch 把 `element_id` 改为可选（默认 `None`），并在 `django/utils/html.py` 的 `json_script` 中按是否有 id 选择两种模板：有 id 时 `<script id="{}" type="application/json">{}</script>`，无 id 时 `<script type="application/json">{}</script>`。`defaultfilters.py` 的过滤器同步把签名改为 `element_id=None`。

## Golden Patch 语义分析

```python
def json_script(value, element_id=None):
    ...
    if element_id:
        template = '<script id="{}" type="application/json">{}</script>'
        args = (element_id, mark_safe(json_str))
    else:
        template = '<script type="application/json">{}</script>'
        args = (mark_safe(json_str),)
    return format_html(template, *args)
```
核心语义：**根据 `element_id` 真值分流模板与参数元组**。有 id 时模板含 `id="{}"` 且 args 含 element_id；无 id 时模板不含 id 属性且 args 只有 json 串。两者必须**模板占位符数量与 args 长度一致**，否则 `format_html` 报错。

F2P 测试：`JsonScriptTests.test_without_id`（模板过滤器 `{{ value|json_script }}` 渲染出无 id 的 script 标签）、`TestUtilsHtml.test_json_script_without_id`（`json_script({'key':'value'})` 输出无 id 标签）。

## 调用链分析

模板过滤器 `json_script(value, element_id=None)`（defaultfilters）→ `_json_script`（即 utils.html.json_script）。`format_html(template, *args)` 要求 template 中 `{}` 的数量与 args 数量匹配。有 id 路径仍被既有测试 `test_json_script`（用 `'test_id'`）覆盖，故"只破坏无 id 路径、保留有 id 路径"的变异 blast radius 最小、最隐蔽。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| B | 🔴 必须替换 | 替换 | 原 `if element_id`→`if not element_id`，同时破坏有 id 与无 id 两条路径（blast radius 6），过猛 |
| C | 🟡→替换 | 替换 | 原删除 else 分支，无 id 时 template/args 未定义→NameError，崩溃式 |
| D | 🔴 必须替换 | 替换 | 原 collapse 成永远走有 id 分支，等价于还原"强制 id"的旧行为 |
| E | 🟢 高质量 | 保留 | 原在函数开头 `if element_id is None: raise TypeError`，但稍显生硬（带显式 raise）；改为更隐蔽的空 id 属性 |

四组均替换为**只破坏无 id 路径、保留有 id 路径**的最小 blast radius 变异（实测每个仅 1 处失败）。

## 各组 Mutation 分析

### Group B — 替换（B1 off-by-one 边界）
**原**：`if element_id:`→`if not element_id:`（两条路径全反，blast radius 6）。
**最终 mutation**：
```diff
-    if element_id:
+    if len(element_id or '') >= 0:
```
**变异语义**：把真值判断换成 `len(element_id or '') >= 0`——长度恒 `>= 0`，**条件永远为真**，于是总是走"有 id"分支。无 id 调用（element_id=None）也进入 id 分支，模板含 `id="{}"` 而 args 为 `(None, json)`，渲染出 `<script id="None"...>` 之类，与期望的无 id 标签不符。模拟"用 len 判空时把 `> 0` 误写成 `>= 0`"的经典 off-by-one。只破坏无 id 路径（有 id 路径本就走此分支，行为不变），blast radius 1。

### Group C — 替换（C-数据形状：args 错配）
**原**：删 else 分支 → NameError。
**最终 mutation**：
```diff
     else:
-        template = '<script type="application/json">{}</script>'
-        args = (mark_safe(json_str),)
+        template = '<script id="{}" type="application/json">{}</script>'
+        args = (element_id, mark_safe(json_str),)
```
**变异语义**：无 id 分支错误地复用了**有 id 的模板与 args**（element_id=None）。结果无 id 调用渲染出 `<script id="None" type="application/json">...`，与期望不符。比原"删 else 导致 NameError"自然——分支结构完整、模板/args 长度自洽（都含 id），只是语义错了。blast radius 1。

### Group D — 替换（D1 状态/默认值处理）
**原**：collapse 成永远有 id（还原旧强制 id 行为）。
**最终 mutation**：
```diff
     json_str = json.dumps(value, cls=DjangoJSONEncoder).translate(_json_script_escapes)
+    element_id = element_id or ''
     if element_id is not None:
```
**变异语义**：先把 `element_id` 规整为 `element_id or ''`（None→空串），再用 `is not None` 判断。空串 `''` 不是 None，故条件恒真，总走有 id 分支，无 id 调用渲染出 `<script id="" ...>`（空 id 属性）。模拟"先给默认值、再用 `is not None` 判断"的状态初始化误用——规整后 `is not None` 失去意义。blast radius 1。

### Group E — 保留思路，改为更隐蔽写法（E1 测试期望）
**原**：函数开头 `if element_id is None: raise TypeError(...)`（显式 raise，略生硬）。
**最终 mutation**：
```diff
-        template = '<script type="application/json">{}</script>'
-        args = (mark_safe(json_str),)
+        template = '<script id="" type="application/json">{}</script>'
+        args = (mark_safe(json_str),)
```
**变异语义**：无 id 分支的模板硬编码了一个**空 id 属性** `id=""`，args 仍只有 json 串（与占位符数量一致，不报错）。输出 `<script id="" type="application/json">{}</script>`，与期望的 `<script type="application/json">{}</script>` 不等，精确断言失败。比原"开头 raise TypeError"更隐蔽——看起来只是"保留一个空 id 占位"，无异常、无明显矛盾。blast radius 1。

## 新设计 Mutation 说明

四个替代统一遵循"**只破坏无 id 路径、保留有 id 路径**"的原则（实测每个仅令无 id 的 F2P 失败、不影响既有的有 id 测试），覆盖四种机制：B 真值判断的 off-by-one、C 复用有 id 模板/args、D 默认值规整后 `is not None` 失效、E 硬编码空 id 属性。相比原始 mutation（B 全反、C 的 NameError、D 的旧行为还原），这组更聚焦、更自然。全部实测：golden 通过、变异令 F2P 失败、`base→golden→test_patch` 后干净应用。
