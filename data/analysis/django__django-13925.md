# django__django-13925

## 问题背景

Django 3.2 引入了 `models.W042` 系统检查：当模型未显式定义主键类型、依赖默认 `AutoField` 自动生成主键时发出警告，提示配置 `DEFAULT_AUTO_FIELD`。但该检查存在 bug——对于**继承自父模型**的子模型，子模型的主键实际上是一个指向父模型的 `parent_link` 类型 `OneToOneField`（自动创建），它本身被标记为 `auto_created`，于是 W042 在子模型上也被错误触发。用户期望：继承来的主键只应在父模型处检查一次，子模型不应重复报警。

Golden patch 在 `_check_default_pk` 的条件中加入一个排除项：当主键是 `parent_link` 的 `OneToOneField`（即继承链接）时，跳过该警告。

## Golden Patch 语义分析

```python
def _check_default_pk(cls):
    if (
        cls._meta.pk.auto_created and
        # Inherited PKs are checked in parents models.
        not (
            isinstance(cls._meta.pk, OneToOneField) and
            cls._meta.pk.remote_field.parent_link
        ) and
        not settings.is_overridden('DEFAULT_AUTO_FIELD') and
        not cls._meta.app_config._is_default_auto_field_overridden
    ):
        return [checks.Warning(..., obj=cls, id='models.W042')]
    return []
```

核心语义：
1. `cls._meta.pk.auto_created` — 主键是 Django 自动创建的（用户没显式定义）。
2. **新增排除项** `not (isinstance(pk, OneToOneField) and pk.remote_field.parent_link)` — 若主键是继承产生的 `parent_link` O2O，则**不**报警（继承的主键在父模型处已检查）。
3. 后两项分别检查 `DEFAULT_AUTO_FIELD` 设置和 app config 是否被覆盖。

四项以 `and` 连接，全为真才返回 W042 警告。

## 调用链分析

- `_check_default_pk` 是 `Model` 的类方法，由 `check_all_models`（系统检查框架）对每个已注册模型调用。
- `cls._meta.pk` 是模型的主键字段对象。对继承的子模型，元类 `ModelBase.__new__`（base.py:243 附近）会自动创建一个 `OneToOneField(parent, parent_link=True)` 作为子模型主键，其 `remote_field`（一个 `OneToOneRel`）的 `parent_link=True`、`multiple=False`。
- 数据流关键：`remote_field` 是 `ForeignObjectRel` 子类实例，`.parent_link` 标记继承链接，`.multiple` 对 O2O 恒为 `False`。
- F2P 测试覆盖 4 个场景：显式继承主键 / 显式 parent_link / 自动创建继承主键 / 自动创建 parent_link，分别验证子模型不报警、警告只挂在父模型上。任何让排除逻辑失效或让警告对象/类型错误的变异都会使这些断言失败。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原 mutation 把 `parent_link` 改为 `False`，功能等价于直接还原 golden patch（排除项恒假），属功能等价冗余 |
| B | 🔴 必须替换 | 替换 | 原 mutation 删除整个排除块，与 golden patch 逆操作字节级等同，直接冗余 |
| C | 🔴 必须替换 | 替换 | 同 B，删除整个排除块，直接冗余 |
| D | 🔴 必须替换 | 替换 | 同 B，删除整个排除块，直接冗余 |

> 注：本实例 mutations.jsonl 中只有 4 个 mutation（无 E 组）。原 A 为功能等价冗余，原 B/C/D 为三份字节级相同的直接还原。4 个全部 🔴 必须替换。

语义浅层共 0 个；必须替换 4 个（A/B/C/D），全部替换为高质量变异。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：
```diff
                 isinstance(cls._meta.pk, OneToOneField) and
-                cls._meta.pk.remote_field.parent_link
+                False
```
**分类**：🔴 必须替换（功能等价冗余）
**理由**：把 `parent_link` 替换为字面量 `False`，使 `isinstance(...) and False` 恒为假，`not (False)` 恒为真——排除项完全失效，等价于 golden patch 从未存在。这就是 golden 修复的功能等价逆操作，且 `and False` 是明显人工痕迹。

**最终 mutation**：
```diff
             return [
-                checks.Warning(
+                checks.Error(
                     f"Auto-created primary key used when not defining a "
```
**变异语义**：把告警级别从 `checks.Warning` 改为 `checks.Error`。排除逻辑、消息文案、`obj`、`id='models.W042'` 全部保持不变，因此凡是该报警的场景仍然报，凡是被正确排除的继承场景仍被排除——**功能行为看似完全正确**。但 W042 在 Django 中契约上是 *Warning* 级别；测试用 `Warning(...)` 构造期望对象，`Error` 实例与之不相等，断言失败。这是一个把"诊断分类"悄悄升级的契约违反：审查者只看逻辑分支会觉得无误，唯有注意到检查框架对 Warning/Error 的语义区分才能发现。属 A3（替换异常/告警类型）。F2P 中三个产生警告的场景（`test_auto_created_pk`、`test_auto_created_inherited_pk`、`test_auto_created_inherited_parent_link`）失败。

### Group B — 替换
**原 mutation**：
```diff
             cls._meta.pk.auto_created and
-            # Inherited PKs are checked in parents models.
-            not (
-                isinstance(cls._meta.pk, OneToOneField) and
-                cls._meta.pk.remote_field.parent_link
-            ) and
             not settings.is_overridden('DEFAULT_AUTO_FIELD') and
```
**分类**：🔴 必须替换（直接冗余）
**理由**：删除整个排除块，是 golden patch 的字节级逆操作。

**最终 mutation**：
```diff
                 isinstance(cls._meta.pk, OneToOneField) and
-                cls._meta.pk.remote_field.parent_link
+                not cls._meta.pk.remote_field.parent_link
```
**变异语义**：在 `parent_link` 前加一个 `not`，把排除条件的语义反转。原意"主键是 parent_link 继承链接时排除"，变成"主键是 O2O 但**不是** parent_link 时排除"。由于真实继承主键的 `parent_link` 恒为 `True`，反转后继承场景不再被排除（子模型重新被错误报警），而那些理论上 `parent_link=False` 的 O2O 主键反被排除。表面是个合理的布尔条件，单看不易判断方向对错；只有结合"自动创建的继承主键 parent_link 一定为真"这一领域知识才能识破。属 B3（反转布尔逻辑/比较）。F2P 中 `test_auto_created_inherited_pk`、`test_explicit_inherited_pk` 失败。

### Group C — 替换
**原 mutation**：（同 B，删除整个排除块，直接冗余）
**分类**：🔴 必须替换（直接冗余）

**最终 mutation**：
```diff
                 isinstance(cls._meta.pk, OneToOneField) and
-                cls._meta.pk.remote_field.parent_link
+                cls._meta.pk.remote_field.multiple
```
**变异语义**：把读取的属性从 `parent_link` 换成同一 `remote_field` 对象上的 `multiple`。两者都是 `ForeignObjectRel` 的布尔属性，名字相近、访问形式完全一致，看起来只是"换了个判断维度"。但 `multiple` 对 `OneToOneRel` 恒为 `False`（一对一关系不允许多重），所以 `isinstance(O2O) and False` 恒假，排除项失效，继承主键重新被报警。这是利用了对关系对象属性语义的混淆——审查者若不清楚 `multiple` 在 O2O 上恒为 False，会以为这是个合理的关系判断。属 C1（破坏隐式类型/形态约定，错误依赖关系对象属性）。F2P 中 `test_auto_created_inherited_pk`、`test_explicit_inherited_pk` 失败。

### Group D — 替换
**原 mutation**：（同 B，删除整个排除块，直接冗余）
**分类**：🔴 必须替换（直接冗余）

**最终 mutation**：
```diff
         if (
-            cls._meta.pk.auto_created and
+            cls._meta.auto_created and
```
**变异语义**：把判断对象从 `cls._meta.pk.auto_created`（**主键字段**是否自动创建）改成 `cls._meta.auto_created`（**模型本身**是否由 Django 自动创建，如 M2M 中间表、proxy 等）。两个属性都叫 `auto_created`、都挂在 `_meta` 系下，仅差一层 `.pk`，极易被看成等价简写。但语义完全不同：普通用户模型的 `_meta.auto_created` 恒为 `False`，于是整个 W042 检查对所有正常模型都不再触发——警告被全面静默。bug 的根因（读错了 `auto_created` 的归属对象）与表现（连最基本的 `test_auto_created_pk` 都不报警）相隔一层属性访问，是典型的"状态/属性归属混淆"。属 D1（状态来源错误，读取了未正确初始化为目标语义的属性）。F2P 中三个产生警告的场景全部失败。

## 新设计 Mutation 说明

四个变异分布在 `_check_default_pk` 的不同语义维度，互不重叠：
- **A**：告警**类型**维度（Warning→Error），逻辑分支完全正确，只违反诊断级别契约。
- **B**：排除条件的**布尔方向**维度（parent_link 取反）。
- **C**：关系对象的**属性选择**维度（parent_link→multiple，依赖 O2O 恒 False 的特性）。
- **D**：触发条件的**属性归属**维度（pk.auto_created→meta.auto_created），影响所有模型。

全部仅修改 `django/db/models/base.py`（允许文件），不触碰测试。均通过 Step 5 实证自查：在 base_commit → golden patch → test_patch 之后可干净应用、`py_compile` 通过，并实际运行 `ModelDefaultAutoFieldTests`（8 个测试）确认每个变异都使至少一个 F2P 测试失败（A/D 各 3 个失败，B/C 各 2 个失败）。
