# django__django-15127

## 问题背景

`@override_settings(MESSAGE_TAGS=...)` 在测试中改变消息标签时，`django.contrib.messages.storage.base.LEVEL_TAGS` 这个模块级常量不会随之更新，导致 `Message.level_tag` 取到空串、识别不到新标签。旧代码靠测试侧自定义的 `override_settings_tags` 手动同步。Golden patch 在 messages app 的 `ready()` 中连接 `setting_changed` 信号，收到 `MESSAGE_TAGS` 变更时调用 `get_level_tags()` 重算并写回 `base.LEVEL_TAGS`，从而让生产代码自动响应设置变更。

## Golden Patch 语义分析

```python
def update_level_tags(setting, **kwargs):
    if setting == 'MESSAGE_TAGS':
        base.LEVEL_TAGS = get_level_tags()

class MessagesConfig(AppConfig):
    ...
    def ready(self):
        setting_changed.connect(update_level_tags)
```
核心语义有四个协同环节：(1) `ready()` 必须真正 `connect` 信号；(2) receiver 必须用正确的 setting 名 `'MESSAGE_TAGS'` 过滤；(3) 必须**调用** `get_level_tags()`（返回 dict）；(4) 必须写回**正确的属性** `base.LEVEL_TAGS`。任何一环断裂，`base.LEVEL_TAGS` 都不会被更新。

F2P 测试 `TestLevelTags.test_override_settings_level_tags`：在 `@override_settings(MESSAGE_TAGS=message_tags)` 下断言 `base.LEVEL_TAGS == message_tags`。

## 调用链分析

`get_level_tags()`（在 `utils.py`）返回 `{**constants.DEFAULT_TAGS, **settings.MESSAGE_TAGS}`。`@override_settings` 触发 `setting_changed` 信号 → 已连接的 `update_level_tags` 收到 `setting='MESSAGE_TAGS'` → 重算并赋值 `base.LEVEL_TAGS`。`Message.level_tag` 读取 `base.LEVEL_TAGS`。测试直接断言 `base.LEVEL_TAGS`。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原 `'MESSAGE_TAGS'`→`'MESSAGE_TAG'`，与 C 字节级重复 |
| B | 🟢 高质量 | 保留 | `==`→`!=`，receiver 对所有非 MESSAGE_TAGS 设置触发，对 MESSAGE_TAGS 反而不更新，且污染其他设置 |
| C | 🔴 必须替换 | 替换 | 与 A 完全相同（同一 typo） |
| D | 🔴 必须替换 | 替换 | 原带 "Bug:" 注释且删掉了 connect 调用，人工痕迹明显 |
| E | 🟢 高质量 | 保留 | `ready(self, auto_connect_signals=False)` 把信号连接藏到一个默认关闭的参数后，ready 实际不连接 |

A、C 同一 typo 重复，D 含 "Bug" 注释，均必须替换；B、E 是不同机制的高质量变异，保留。三个被替换/新增的组（A、C、D）改用语义各异的写法。

## 各组 Mutation 分析

### Group A — 替换（A1 setting 名 typo）
**原**：`if setting == 'MESSAGE_TAG':`（与 C 重复）。
**最终 mutation**：保持 setting 名 typo 思路但作为 A 的代表（与 C 区分见下）：
```diff
-    if setting == 'MESSAGE_TAGS':
+    if setting == 'MESSAGE_TAG':
```
**变异语义**：receiver 用错误的 setting 名 `'MESSAGE_TAG'`（漏掉复数 S）过滤，`override_settings(MESSAGE_TAGS=...)` 发出的信号 `setting` 是 `'MESSAGE_TAGS'`，永不匹配，故 `base.LEVEL_TAGS` 不更新。模拟常量名手误。
（注：A 与 C 在重新分配后改为承载不同机制——见 C。）

### Group B — 保留（B3 条件反转）
```diff
-    if setting == 'MESSAGE_TAGS':
+    if setting != 'MESSAGE_TAGS':
```
**变异语义**：把过滤条件取反。MESSAGE_TAGS 变更时 receiver 反而不更新（条件为假），而任意其它设置变更时却用当前（可能未覆盖的）MESSAGE_TAGS 重算并写回。F2P 下 `base.LEVEL_TAGS` 不随 MESSAGE_TAGS 更新，断言失败。保留。

### Group C — 替换（C1 类型/数据形状：未调用）
**最终 mutation**：
```diff
-        base.LEVEL_TAGS = get_level_tags()
+        base.LEVEL_TAGS = get_level_tags
```
**变异语义**：把 `get_level_tags()`（调用，返回 dict）误写成 `get_level_tags`（函数对象本身）。`base.LEVEL_TAGS` 被赋成一个函数而非标签字典，类型完全错误，断言 `base.LEVEL_TAGS == message_tags` 失败。模拟"忘了加括号"的经典调用遗漏——数据形状从 dict 变成 callable。与 A 的 setting-名 typo 是不同机制。

### Group D — 替换（D1 状态传播：写错属性）
**原**：带 "Bug:" 注释、用 `_signal_connected` 标志且删掉 connect。
**最终 mutation**：
```diff
-        base.LEVEL_TAGS = get_level_tags()
+        base.LEVEL_TAG = get_level_tags()
```
**变异语义**：receiver 把结果写到 `base.LEVEL_TAG`（漏掉复数 S 的属性名），于是真正被读取的 `base.LEVEL_TAGS` 永不更新——状态被写进了一个无人读取的新属性。比原 mutation（"Bug" 注释 + 删 connect）自然得多，模拟属性名 typo 导致的状态传播断裂。

### Group E — 保留（E2 隐式→显式参数）
```diff
-    def ready(self):
-        setting_changed.connect(update_level_tags)
+    def ready(self, auto_connect_signals=False):
+        if auto_connect_signals:
+            setting_changed.connect(update_level_tags)
```
**变异语义**：给 `ready` 加一个默认 `False` 的开关参数，把信号连接藏在其后。AppConfig 框架调用 `ready()` 时不传该参数 → 默认 False → 信号永不连接 → `update_level_tags` 从不触发。模拟"把行为做成可配置、但默认关掉"的隐式→显式退化。保留。

## 新设计 Mutation 说明

A（setting 名 typo，过滤永不匹配）、C（漏括号，LEVEL_TAGS 变成 callable）、D（属性名 typo，写到 LEVEL_TAG）三者机制各异，分别打击 golden 修复的"setting 过滤 / 调用求值 / 写回属性"三个环节，替换掉原来重复的 typo（A=C）和带 "Bug" 注释的 D。B、E 作用于"条件逻辑"与"信号连接"两个不同环节，保留。全部实测：golden 通过、变异令 F2P 失败、`base→golden→test_patch` 后干净应用。
