# django__django-17087

## 问题背景

嵌套类的类方法不能用作 `Field.default`——迁移序列化时丢了中间类名。如 `Profile.Capability.default`（Capability 是 Profile 的嵌套类），迁移文件却序列化成 `appname.models.Capability.default`（少了 `Profile.`），migrate 时报错。根因：`FunctionTypeSerializer.serialize` 用 `klass.__name__`（仅末段类名 `Capability`）拼接路径，而非 `klass.__qualname__`（全限定 `Profile.Capability`）。Golden patch 把 `klass.__name__` 改成 `klass.__qualname__`。

## Golden Patch 语义分析

```python
klass = self.value.__self__
module = klass.__module__
return "%s.%s.%s" % (module, klass.__qualname__, self.value.__name__), {
    "import %s" % module
}
```
核心语义：**序列化绑定到类的方法时，类名部分必须用 `klass.__qualname__`（含外层类的全限定名，如 `Profile.Capability`）而非 `klass.__name__`（仅 `Capability`）**。方法名部分仍用 `self.value.__name__`（如 `default`/`method`）。三段拼接 `module.qualname.method_name`。`__qualname__` 是唯一修复点——它对嵌套类给出 `Outer.Inner`，顶层类则与 `__name__` 相同。

F2P 测试 `WriterTests.test_serialize_nested_class_method`：序列化 `NestedChoices.method`（NestedChoices 是 WriterTests 的嵌套类），断言结果为 `migrations.test_writer.WriterTests.NestedChoices.method`。

## 调用链分析

`FunctionTypeSerializer.serialize`：`self.value` 是绑定方法，`self.value.__self__` 是其所属类 klass，`klass.__qualname__` 给全限定类名，`self.value.__name__` 给方法名，拼成 `module.qualname.method`。`__name__`/`__qualname__` 用错、丢段、方法名取错属性、参数顺序错、或门控开关，都会让嵌套类路径错误。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `klass.__qualname__`→`klass.__name__`（还原 bug） |
| B | 🔴 必须替换 | 替换 | 原 B 与 A 相同；改为丢掉 klass 段 |
| C | 🔴 必须替换 | 替换 | 原 C 与 A 相同；改为 `self.value.__qualname__`（方法段取错） |
| D | 🔴 必须替换 | 替换 | 原 D 与 A 相同；改为参数顺序对调 |
| E | 🟢 高质量 | 保留（重做）| `__qualname__` 藏到 `use_qualname` 开关后 |

原始 A/B/C/E 四组全部字节相同（`klass.__qualname__`→`klass.__name__`），只有一种机制。本 patch 是单行三参数格式化，保留 A（还原 bug），重做 B（丢 klass 段）、C（方法段取错属性）、D（参数顺序对调）、E（默认关闭开关）。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：__qualname__→__name__）
```diff
-            return "%s.%s.%s" % (module, klass.__qualname__, self.value.__name__), {
+            return "%s.%s.%s" % (module, klass.__name__, self.value.__name__), {
```
**变异语义**：`klass.__qualname__` 改回 `klass.__name__`，正是原始 bug——嵌套类只取末段类名（`NestedChoices`，而非 `WriterTests.NestedChoices`），序列化路径丢失外层类 `WriterTests.`。F2P 断言完整全限定路径 → 失败。保留。

### Group B — 替换（D1 状态：丢掉 klass 段）
**原**：与 A 相同。
**最终 mutation**：
```diff
-            return "%s.%s.%s" % (module, klass.__qualname__, self.value.__name__), {
+            return "%s.%s" % (module, self.value.__name__), {
```
**变异语义**：格式串从三段 `%s.%s.%s` 减为两段 `%s.%s`，并从参数里去掉 `klass.__qualname__`——序列化结果只剩 `module.method_name`（如 `migrations.test_writer.method`），**完全丢失类名段**。比 A（类名仅缺外层）更彻底——整个类路径都没了。模拟"重构格式串时少写一段、连类名一起丢了"。F2P 失败。重做为 B。

### Group C — 替换（A1 接口契约：方法段取错属性）
**原**：与 A 相同。
**最终 mutation**：
```diff
-            return "%s.%s.%s" % (module, klass.__qualname__, self.value.__name__), {
+            return "%s.%s.%s" % (module, klass.__qualname__, self.value.__qualname__), {
```
**变异语义**：方法名段 `self.value.__name__` 改成 `self.value.__qualname__`。绑定方法的 `__qualname__` 含其类前缀（如 `WriterTests.NestedChoices.method`），与前面已拼的 `klass.__qualname__`（`WriterTests.NestedChoices`）组合后，路径里类名段**重复**——变成 `module.WriterTests.NestedChoices.WriterTests.NestedChoices.method`。模拟"方法段也误用了 qualname、导致类前缀重复"。比 A 隐蔽——类名段是对的、错在方法段。F2P 失败。重做为 C。

### Group D — 替换（D4 状态：参数顺序对调）
**原**：与 A 相同。
**最终 mutation**：
```diff
-            return "%s.%s.%s" % (module, klass.__qualname__, self.value.__name__), {
+            return "%s.%s.%s" % (module, self.value.__name__, klass.__qualname__), {
```
**变异语义**：格式化参数里 `klass.__qualname__` 与 `self.value.__name__` 顺序对调——类名段与方法名段位置互换，结果如 `module.method.WriterTests.NestedChoices`（方法名跑到类名前面）。占位数不变、不报错，但路径结构错位。模拟"格式化参数顺序写反"。F2P 失败。重做为 D。

### Group E — 保留/重做（E2 隐式→显式开关）
**原**：与 A 字节相同。
**最终 mutation**：
```diff
-            return "%s.%s.%s" % (module, klass.__qualname__, self.value.__name__), {
+            return "%s.%s.%s" % (module, klass.__qualname__ if getattr(self, "use_qualname", False) else klass.__name__, self.value.__name__), {
```
**变异语义**：类名段用 `klass.__qualname__ if getattr(self, "use_qualname", False) else klass.__name__`——`use_qualname` 开关默认 False → 走 `klass.__name__`（原 bug，仅末段类名）。只有显式开启才用全限定名。默认序列化嵌套类丢中间类。模拟"把全限定名序列化做成可配置、默认却关掉"。重做为 E。

## 新设计 Mutation 说明

原始 A/B/C/E 四组字节完全相同（`klass.__qualname__`→`klass.__name__`），实际只有"用 `__name__` 还原 bug"一种机制。本 patch 是单行三参数格式化，五组围绕"类名段取错属性 / 丢掉类名段 / 方法段取错属性 / 参数顺序 / 默认关闭开关"分化：保留 A（`__name__` 还原 bug），重做 B（丢 klass 段，只剩 module.method）、C（方法段误用 `__qualname__`，类前缀重复）、D（类名段与方法段参数顺序对调）、E（`use_qualname` 默认关闭开关）。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
