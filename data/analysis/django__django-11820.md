# django__django-11820

## 问题背景

Django 的 `Model._check_ordering()` 方法用于验证 `Meta.ordering` 中指定的字段是否合法。该 issue 报告了两个关联 bug：

1. 当 ordering 使用 `related_field__pk` 形式时（如 `parent__pk`），校验会错误地报告 `models.E015`（字段不存在），因为 `pk` 是一个别名，不能通过 `opts.get_field('pk')` 查找。
2. 当 ordering 使用 `related_field__non_relation_field__extra` 形式时（如 `parent__field1__field2`，其中 `field1` 是 CharField），校验不会报告错误，因为遍历到非关系字段后 `_cls` 未重置为 `None`，导致后续部分继续从旧的 `_cls` 上查找字段。

## Golden Patch 语义分析

Golden patch 修复了两处独立的逻辑缺陷：

**修复1（pk 别名）**：在遍历 ordering 字段的各部分时，增加对 `part == 'pk'` 的特殊处理，直接使用 `_cls._meta.pk` 获取主键字段，而非调用 `get_field('pk')`（后者会抛出 `FieldDoesNotExist`）。

**修复2（非关系字段后重置 `_cls`）**：在 `if fld.is_relation:` 的 `else` 分支新增 `_cls = None`，当遍历到非关系字段时将 `_cls` 清空。这样下一次循环对 `None._meta.get_field(...)` 会抛出 `AttributeError`，被 except 子句捕获并报告错误，而不会错误地继续在旧 `_cls` 上查找字段。

两处修复协同作用：修复1保证 `pk` 别名可以正常解析；修复2保证非关系字段之后不能再继续做关系遍历。

## 调用链分析

```
Model._check() [base.py ~1200]
  └── Model._check_ordering(cls) [base.py ~1672]
        ├── cls._meta.ordering — 获取 Meta.ordering 配置
        ├── 分离 related_fields（含 LOOKUP_SEP）和 _fields（非关系）
        ├── 对每个 related_field 遍历 LOOKUP_SEP 分割后的 parts：
        │     ├── _cls._meta.get_field(part) — 查找字段
        │     ├── fld.is_relation → _cls = fld.get_path_info()[-1].to_opts.model
        │     └── FieldDoesNotExist/AttributeError → 检查 fld.get_transform(part)
        └── 对每个 non-related field 检查是否在 valid_fields 集合中
```

被修改函数 `_check_ordering` 是一个纯校验函数，没有外部调用者在业务逻辑中依赖它的返回值（只用于 system check framework）。但它的行为直接决定了用户运行 `python manage.py check` 时是否会看到误报或漏报。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | `and False` 是显式永假条件，人工痕迹极强，任何代码审查者立即发现 |
| B | 语义浅层 | 保留 | `_cls == cls` 限制 pk 别名仅对顶层 class 生效，位置关键，边界语义有意义 |
| C | 必须替换 | 替换 | `pk_DISABLED` 是虚构字符串，不符合任何 Django 字段命名规范，明显人工痕迹 |
| D | 高质量 | 保留 | 删除 `else: _cls = None`，使非关系字段后遍历不中止，修改位置不同于其他 mutation |
| E | 必须替换 | 替换 | 直接还原 golden patch 中的 pk 别名处理逻辑（逆操作），属于直接冗余 |

语义浅层共 1 个（B），替换其中最弱的 floor(1/2) = 0 个：无需替换语义浅层 mutation。

总替换：3 个（A、C、E）。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
-                    if part == 'pk':
+                    if part == 'pk' and False:
                         fld = _cls._meta.pk
```
**分类**：🔴 必须替换

**理由**：`and False` 使条件永远为假，等同于完全移除 pk 别名处理，但写法极不自然。`and False` 是调试时临时禁用代码的常见手法，任何有经验的代码审查者都会立即质疑。这类 mutation 无法通过真实的代码审查。

**最终 mutation**：
```diff
-                    if part == 'pk':
+                    if part == _cls._meta.pk.name:
                         fld = _cls._meta.pk
```
**变异语义**：将 `pk` 别名检测从字面字符串比较改为与实际主键字段名（通常是 `'id'`）比较。`ordering = ('parent__pk',)` 中 `part = 'pk'` 而 `_cls._meta.pk.name = 'id'`，条件不满足，导致 `get_field('pk')` 抛 `FieldDoesNotExist`，产生误报错误。对使用实际字段名（如 `parent__id`）的 ordering 则不受影响。代码看起来像是"更精确的 pk 检测"，难以被浅层审查发现。`test_ordering_pointing_to_related_model_pk` 会失败，`test_ordering_pointing_multiple_times_to_model_fields` 仍通过。

---

### Group B — 保留

**原 mutation**：
```diff
-                    if part == 'pk':
+                    if part == 'pk' and _cls == cls:
                         fld = _cls._meta.pk
```
**分类**：🟡 语义浅层（保留）

**理由**：`_cls == cls` 意味着只对顶层 class 处理 pk 别名，对关联模型的 pk（如 `parent__pk` 中的 `pk`）不做特殊处理。这修改了 pk 别名解析的作用范围，语义清晰且位于关键条件判断节点。虽然是单行修改，但条件添加具有明确的语义差异，不与其他 mutation 重复，值得保留。

**最终 mutation**（与原 mutation 相同）：
```diff
-                    if part == 'pk':
+                    if part == 'pk' and _cls == cls:
                         fld = _cls._meta.pk
```
**变异语义**：当 `_cls` 已经通过关系遍历更新为关联模型时（如处理 `parent__pk` 中的 `pk` 时 `_cls = Parent ≠ cls = Child`），pk 别名处理不生效，`get_field('pk')` 失败，产生误报。仅对顶层 class 直接使用 pk 排序（如 `ordering = ('pk',)` 中不含 `LOOKUP_SEP`，因此不走这个代码路径）不受影响；实际上只影响关联模型的 pk 排序。

---

### Group C — 替换

**原 mutation**：
```diff
-                    if part == 'pk':
+                    if part == 'pk_DISABLED':
                         fld = _cls._meta.pk
```
**分类**：🔴 必须替换

**理由**：`'pk_DISABLED'` 是一个不存在于 Django 字段系统中的虚构字符串，显然是人为拼接用于"禁用"某功能的命名，不符合任何代码风格。任何代码审查者在读到这一行时都会立即注意到。

**最终 mutation**：
```diff
-                except (FieldDoesNotExist, AttributeError):
+                except FieldDoesNotExist:
                     if fld is None or fld.get_transform(part) is None:
```
**变异语义**：移除 `AttributeError` 的捕获。在 golden patch 后的代码中，当 `_cls = None`（非关系字段后重置）后继续遍历下一个 part 时，`None._meta.get_field(part)` 会抛出 `AttributeError`。不捕获该异常会导致异常向上传播，使 `check()` 调用崩溃而非返回 `[Error(...)]`。这对 `test_ordering_pointing_multiple_times_to_model_fields` 致命，但不影响 `test_ordering_pointing_to_related_model_pk`（pk 处理成功，不进入 except）。修改看起来像合理的"简化异常处理"，难以浅层检测。

---

### Group D — 保留

**原 mutation**：
```diff
                         _cls = fld.get_path_info()[-1].to_opts.model
-                    else:
-                        _cls = None
```
**分类**：🟢 保留

**理由**：删除 `else: _cls = None` 使非关系字段后 `_cls` 保持为之前的模型，导致后续部分在错误的 class 上查找字段。修改位置与其他 mutation 完全不同（影响 `_cls` 重置逻辑而非 pk 检测），为多行删除，模拟真实开发者"忘记在非关系节点重置遍历上下文"的错误。

**最终 mutation**（与原 mutation 相同）：
```diff
                         _cls = fld.get_path_info()[-1].to_opts.model
-                    else:
-                        _cls = None
```
**变异语义**：对 `parent__field1__field2`，处理 `field1`（CharField）后 `_cls` 保持为 `Parent`，`field2` 在 `Parent` 上成功找到 → 不报错。测试期望报错 → `test_ordering_pointing_multiple_times_to_model_fields` 失败。对 `parent__pk` 无影响（pk 是 pk special case，`fld.is_relation = False`，`_cls = None`，但循环已结束）。

---

### Group E — 替换

**原 mutation**：
```diff
-                    # pk is an alias that won't be found by opts.get_field.
-                    if part == 'pk':
-                        fld = _cls._meta.pk
-                    else:
-                        fld = _cls._meta.get_field(part)
+                    fld = _cls._meta.get_field(part)
```
**分类**：🔴 必须替换

**理由**：这是对 golden patch 中 pk 别名修复的完整逆操作，即直接还原到 base_commit 的原始代码，属于最直接的冗余 mutation。

**最终 mutation**：
```diff
-                    if fld is None or fld.get_transform(part) is None:
+                    if fld is None or (fld.get_transform(part) is None and fld.is_relation):
                         errors.append(
```
**变异语义**：将错误报告条件从"找不到字段且无对应 transform"改为"找不到字段且该字段是关系字段"。当 `_cls = None` 后遍历 `field2` 时，except 捕获 AttributeError，`fld` 为上一个成功的 CharField（非关系），`fld.is_relation = False`，整个条件为 False → 错误不被追加。`test_ordering_pointing_multiple_times_to_model_fields` 失败（期望错误但无错误）。对 `parent__pk` 无影响（pk 处理成功，不进入 except）。修改逻辑看似合理（"只有关系字段解析失败才算真正的 ordering 错误"），隐蔽性强。

## 新设计 Mutation 说明

**Group A 新 mutation（`_cls._meta.pk.name` 替换 `'pk'`）**：

基于对 `pk` 在 Django Meta Options 中的语义分析。`pk` 是主键的通用别名，`_meta.pk.name` 返回实际主键字段名（通常为 `'id'`）。开发者可能误以为"检测当前 class 的 pk 字段名是否等于 part"才是正确的比较方式，而忽视了 `'pk'` 本身就是通用别名这一事实。该 mutation 只影响使用 `'pk'` 别名的 ordering（最常见场景），对使用实际字段名的 ordering 无影响，因此能通过大多数简单测试。

**Group C 新 mutation（移除 `AttributeError` 捕获）**：

基于对 `_cls = None` 后的代码执行路径分析。`_cls` 被设置为 `None` 是 golden patch 的修复之一，而 `None._meta` 必然抛出 `AttributeError`。若不捕获 `AttributeError`，则异常传播而非被处理为 ordering 错误。开发者可能认为"只有字段查找失败（`FieldDoesNotExist`）才需要处理，`AttributeError` 不应该出现在正常代码路径中"——这是一个合理但有缺陷的假设。

**Group E 新 mutation（错误条件加 `and fld.is_relation`）**：

基于对 except 块内错误追加逻辑的深层分析。原条件 `fld is None or fld.get_transform(part) is None` 覆盖两类情况：首次迭代失败（fld=None）和无法继续遍历（无 transform）。新条件将第二类限制为"只有关系字段的遍历失败才报错"，使非关系字段被错误继续遍历后的失败不产生错误。这模拟了开发者对"什么情况下 ordering 路径非法"的错误理解，隐蔽性高。
