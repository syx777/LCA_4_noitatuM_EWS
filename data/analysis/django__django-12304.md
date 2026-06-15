# django__django-12304

## 问题背景

Django 3.0 引入了枚举类型（`IntegerChoices`、`TextChoices`）作为模型字段的选项类，但这些枚举类不能在 Django 模板中直接使用。原因是 Django 模板引擎（`template/base.py` 的 `_resolve_lookup` 方法）会检测到枚举类是可调用对象（callable），并尝试不带参数地调用它（如 `Suit()`），但枚举构造器需要一个 `value` 参数，因此调用失败，模板最终渲染为空字符串。

例如 `{% if student.year_in_school == YearInSchool.FRESHMAN %}` 无法工作。

Golden patch 的修复方案：在 `ChoicesMeta.__new__` 中为每个枚举子类设置 `cls.do_not_call_in_templates = True`，这是 Django 模板引擎识别的"禁止在模板中调用"的标志位（见 `template/base.py:852`）。

## Golden Patch 语义分析

```diff
+        cls.do_not_call_in_templates = True
```

核心逻辑：Django 模板引擎在 `_resolve_lookup` 中对每个 lookup bit 执行如下检查：
```python
if callable(current):
    if getattr(current, 'do_not_call_in_templates', False):
        pass  # 跳过调用，直接用原值
    elif getattr(current, 'alters_data', False):
        current = context.template.engine.string_if_invalid  # 用无效占位符
    else:
        current = current()  # 尝试无参调用
```

修复在 `ChoicesMeta.__new__` 中将 `do_not_call_in_templates = True` 设置在**枚举类**（`cls`）上，而不是枚举实例上，因为模板引擎检查的是 `current`（即 `Suit` 类本身）。

## 调用链分析

```
ChoicesMeta.__new__                       # 元类，在创建 Suit/IntegerChoices 时执行
  -> cls = super().__new__(...)           # 生成枚举类
  -> cls._value2label_map_ = dict(zip(   # 构建 value->label 映射
         cls._value2member_map_, labels))
  -> cls.label = property(lambda self:   # 在枚举成员上暴露 label 属性
         cls._value2label_map_.get(self.value))
  -> cls.do_not_call_in_templates = True # 告知 Django 模板不要调用此类

Django template._resolve_lookup           # 模板变量解析
  -> callable(Suit) -> True
  -> getattr(Suit, 'do_not_call_in_templates', False)
  -> if True: pass (直接用 Suit 不调用)
  -> getattr(Suit, 'DIAMOND') -> Suit.DIAMOND (枚举成员)
  -> callable(Suit.DIAMOND) -> False (int 实例不可调用)
  -> 渲染 Suit.DIAMOND.label = 'Diamond'
```

`_value2label_map_` 是核心数据结构，`zip(cls._value2member_map_, labels)` 的参数顺序决定了映射方向（value→label）。`label` property 通过 `self.value` 作为 key 进行查找。

## 替换决策总览

| 组 | 原始类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | `True -> False` 是对 golden patch 的直接还原，等价于 base_commit 原始状态 |
| B | 🔴 必须替换 | 替换 | `# cls.do_not_call_in_templates = True` 注释掉代码，不自然，代码审查会立即发现 |
| C | 🔴 必须替换 | 替换 | 整行删除，等价于还原，且 B 和 C 效果相同（都是让 do_not_call 不存在） |
| D | 🔴 必须替换 | 替换 | 与 B 完全相同的注释掉方式（两条记录的 diff 内容一模一样） |
| E | 缺失 | 新增 | mutations.jsonl 中无 E 组，需新建 |

所有4个已有 mutation 均需替换，且全部集中在同一行（`do_not_call_in_templates`），缺乏多样性。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
-        cls.do_not_call_in_templates = True
+        cls.do_not_call_in_templates = False
```
**分类**：🔴 必须替换（直接冗余）

**理由**：`True -> False` 即直接还原 golden patch 的逆操作，语义上等同于 base_commit 原始状态，测试一眼可发现（`False` 是明显错误值）。

**最终 mutation**：
```diff
-        cls._value2label_map_ = dict(zip(cls._value2member_map_, labels))
+        cls._value2label_map_ = dict(zip(labels, cls._value2member_map_))
```

**变异语义**：将 `zip` 的两个参数顺序对调。原意是构建 `value → label` 映射（键为整数值，如 `{1: 'Diamond', 2: 'Spade'}`），对调后变成 `label → value` 映射（键为字符串 `{'Diamond': <Suit.DIAMOND: 1>, ...}`）。`label property` 调用 `_value2label_map_.get(self.value)`（以整数1为键），在新映射中找不到，返回 `None`。代码审查中参数顺序错误极难发现，因为两个参数名称长度接近，类型也是 iterable，只有深入理解语义才能察觉。此 mutation 使 `Suit.DIAMOND.label` 返回 `None`，模板渲染 `{{ Suit.DIAMOND.label }}` 输出 `None`（或空字符串），`test_templates` 失败。

### Group B — 替换

**原 mutation**：
```diff
-        cls.do_not_call_in_templates = True
+        # cls.do_not_call_in_templates = True
```
**分类**：🔴 必须替换（不自然 — 注释掉代码是明显人工痕迹）

**理由**：代码中出现 `# cls.do_not_call_in_templates = True` 这种注释形式在生产代码中极为罕见，代码审查者会立即怀疑。

**最终 mutation**：
```diff
-        cls.label = property(lambda self: cls._value2label_map_.get(self.value))
+        cls.label = property(lambda self: cls._value2label_map_.get(self.name))
```

**变异语义**：lambda 内部将查找键从 `self.value`（整数 `1`）改为 `self.name`（字符串 `'DIAMOND'`）。而 `_value2label_map_` 的键是整数值，不含字符串名称，因此 `.get('DIAMOND')` 始终返回 `None`。这模拟了开发者混淆 enum 的 `.value` 和 `.name` 属性的真实错误——在 Python enum 中，`.name` 是成员名（`'DIAMOND'`），`.value` 才是存储值（`1`）。此 mutation 中 `do_not_call_in_templates = True` 保留正确，只有 label 访问失效，因此 `test_templates` 失败的原因是 label 返回 `None`。

### Group C — 替换

**原 mutation**：
```diff
-        cls.do_not_call_in_templates = True
+        (行删除)
```
**分类**：🔴 必须替换（功能等价冗余 — 与 B 组效果完全相同）

**理由**：删除该行与注释掉该行的运行时效果相同，且 C 组与 B 组原始 mutation 重复。

**最终 mutation**：
```diff
-        cls._value2label_map_ = dict(zip(cls._value2member_map_, labels))
+        cls._value2label_map_ = {m.name: label for m, label in zip(cls, labels)}
```

**变异语义**：将 dict 构建方式从 `value → label`（`_value2member_map_` 键为整数）改为 `name → label`（键为字符串成员名 `'DIAMOND'`、`'SPADE'` 等）。`label property` 仍用 `self.value`（整数）作为键查找，在 `name → label` 映射中找不到，返回 `None`。与 A 组的区别：A 组对调了两个 iterable，C 组则通过 dict comprehension 显式用了 `m.name` 作为键——两种写法都模拟真实开发者可能犯的错误（一个是参数顺序混淆，另一个是用 name 而非 value 作为字典键）。

### Group D — 替换

**原 mutation**：
```diff
-        cls.do_not_call_in_templates = True
+        # cls.do_not_call_in_templates = True
```
**分类**：🔴 必须替换（与 B 组原始 mutation 完全相同，重复）

**理由**：B 和 D 的原始 diff 内容完全一致（git index hash 也相同：`f8690cf89e`），属于直接重复。

**最终 mutation**：
```diff
-        cls.do_not_call_in_templates = True
+        for member in cls:
+            member.do_not_call_in_templates = True
```

**变异语义**：将 `do_not_call_in_templates = True` 设置在**枚举成员实例**上，而非枚举**类**上。Django 模板引擎检查的是 `current`（即 `Suit` 类本身），通过 `getattr(Suit, 'do_not_call_in_templates', False)` 查找。`Suit` 类本身没有此属性（只有成员实例 `Suit.DIAMOND` 有），返回 `False`，导致模板引擎尝试调用 `Suit()`，失败后渲染为 `string_if_invalid`（空字符串）。这模拟了开发者误解"应该在每个枚举成员上设置此标志"的思维误区，同时所有与 label、choices 相关的 P2P 测试完全不受影响（因为它们不通过模板引擎访问枚举）。

### Group E — 新增

**原 mutation**：（不存在）

**分类**：新建（strategy_group E，策略 E1）

**最终 mutation**：
```diff
-        cls.do_not_call_in_templates = True
+        cls.alters_data = True
```

**变异语义**：将 `do_not_call_in_templates` 替换为语义相近的 `alters_data`。Django 模板引擎对这两个标志的处理方式不同：`do_not_call_in_templates=True` 保留对象原样；`alters_data=True` 则将对象替换为 `context.template.engine.string_if_invalid`（通常为空字符串 `''`）。`alters_data` 原本是为数据库写操作（如 `QuerySet.delete()`）设计的，语义上表示"调用此方法会修改数据，模板中不应调用"，与 `do_not_call_in_templates` 的语义相似但结果截然不同。开发者可能在阅读 Django 模板引擎文档时混淆这两个标志。此 mutation 使 `{{ Suit.DIAMOND.label }}` 渲染为 `''`，`test_templates` 失败；所有非模板的 P2P 测试不受影响。

## 新设计 Mutation 说明

### Group A（zip 参数顺序对调）
基于 `ChoicesMeta.__new__` 中第29行 `_value2label_map_` 的构建方式。原代码 `dict(zip(cls._value2member_map_, labels))` 中，第一个参数（迭代 `_value2member_map_` 的键，即整数值）作为 dict 的键，第二个参数（labels 列表）作为 dict 的值。对调后，labels 变成键，整数值变成值，导致 `label property` 的查找永远返回 `None`。选择此位置是因为 `zip()` 的参数顺序错误是一种非常自然的笔误，且从代码上极难区分正确顺序。

### Group B（self.name vs self.value）
基于 `label property` 的 lambda 实现。`_value2label_map_` 以枚举成员的数值（如整数 `1`、字符串 `'FR'`）为键，但将 `self.value` 改为 `self.name` 后，查找键变成了成员名（如 `'DIAMOND'`、`'FRESHMAN'`），在映射中找不到。模拟了开发者对 `enum.name` 和 `enum.value` 语义混淆的错误，在代码审查中不易察觉，因为 lambda 只改了一个单词。

### Group C（name→label 字典构建）
基于 `_value2label_map_` 构建方式的另一角度。使用 `{m.name: label for m, label in zip(cls, labels)}` 构建了 `name → label` 映射（而非 `value → label`），与 B 的突变互补——B 改变的是查找时的 key，C 改变的是构建时的 key 选择。两者都导致 `label property` 返回 `None`，但代码实现路径不同，增加了 mutation 的多样性。

### Group D（实例级 vs 类级属性）
基于对 Python attribute lookup 机制的深入理解。`do_not_call_in_templates` 必须设置在**类**上，才能被 `getattr(Suit, ...)` 找到（Django 模板检查的是类，不是实例）。将其设置在成员实例上是一种合理但错误的理解——开发者可能认为"每个成员都应该有这个标志"。此 mutation 完全不影响 label、choices、containment 等所有其他逻辑，只在模板引擎实际调用时才暴露问题，是最难在代码审查中发现的一类错误。

### Group E（alters_data vs do_not_call_in_templates）
基于 Django 模板引擎两个语义相近但行为截然不同的标志。`alters_data` 和 `do_not_call_in_templates` 都是"阻止模板调用"的机制，但前者输出 `string_if_invalid`（通常为 `''`），后者直接保留原值。开发者阅读 Django 文档时可能误用 `alters_data`，认为"这个类修改数据（枚举是不可变的，但 choices 方法返回数据）"。此 mutation 不影响任何非模板测试，只有在模板渲染时才会产生错误输出。
