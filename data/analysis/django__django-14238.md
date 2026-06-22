# django__django-14238 Mutation 整理分析

## 问题背景

`DEFAULT_AUTO_FIELD` 设置为 `BigAutoField` / `SmallAutoField` 的**子类**（例如用户自定义的
`class MyBigAutoField(models.BigAutoField): pass`）时，Django 在 `Options._get_default_pk_class()`
中执行 `issubclass(pk_class, AutoField)` 校验会失败并抛出
`ValueError: Primary key '...' must subclass AutoField`。

根因在 `django/db/models/fields/__init__.py` 的 `AutoFieldMeta.__subclasscheck__`：
原实现为 `subclass in self._subclasses`，只检测对象是否**正好等于** `BigAutoField` 或
`SmallAutoField`，无法识别它们的子类。

## Golden Patch 语义分析

```python
def __subclasscheck__(self, subclass):
-    return subclass in self._subclasses or super().__subclasscheck__(subclass)
+    return issubclass(subclass, self._subclasses) or super().__subclasscheck__(subclass)
```

把"成员相等判断" (`in`) 改为"子类判断" (`issubclass`)，使任意 `BigAutoField` /
`SmallAutoField` 的派生类在 `issubclass(X, AutoField)` 时返回 `True`。
`_subclasses` 属性返回 `(BigAutoField, SmallAutoField)`，是这两个检查方法共享的数据来源。

## 调用链分析

- `Options._get_default_pk_class()` (`django/db/models/options.py:245`) →
  `issubclass(pk_class, AutoField)`，因 `AutoField` 的元类是 `AutoFieldMeta`，
  触发 `AutoFieldMeta.__subclasscheck__`。
- 模型创建期间 `_prepare()` → `_get_default_pk_class()` 决定主键字段类型。
- F2P 测试覆盖两条路径：
  - `model_fields.test_autofield.AutoFieldInheritanceTests.test_issubclass_of_autofield`：
    直接断言 `issubclass(MyBigAutoField/MySmallAutoField/BigAutoField/SmallAutoField, AutoField)`。
  - `model_options.test_default_pk.TestDefaultPK.test_default_auto_field_setting_bigautofield_subclass`：
    设置 `DEFAULT_AUTO_FIELD` 指向用户子类 `MyBigAutoField`，断言生成主键为该类型。

关键约束：P2P 测试（`test_default_pk` 中 `test_default_auto_field_setting` /
`test_app_default_auto_field` / `test_m2m_*`）依赖**内建** `BigAutoField`/`SmallAutoField`
被正确识别。因此安全的 F2P 失败窗口是**用户自定义子类**这一类输入，不能波及内建字段路径。

## 替换决策总览

| 组 | 原 diff 概述 | 分类 | 决策 | 最终变异机制 |
|----|--------------|------|------|--------------|
| A | `_subclasses` 改为 `(BigAutoField,)` | 🟢 KEEP | 保留 | 不同节点，删除 SmallAutoField 分支 |
| C | `issubclass`→`isinstance` 单 token | 🟡 SHALLOW | 保留 | M=1, floor(1/2)=0，不替换 |
| D | `issubclass`→`subclass in self._subclasses` | 🔴 golden 反向 | 替换 | 加入伪装的模块来源守卫 |
| E | 与 D 完全相同（重复 golden 反向） | 🔴 golden 反向 | 替换 | 改为 `__name__` 字符串匹配 |

shallow 数量 M=1（仅 C），floor(M/2)=0，故 C 保留。A 为不同节点的语义改动保留。
D、E 均为 golden 的直接逆向（且互相重复），属 🔴 必替换。

## 各组 Mutation 分析

### Group A —— KEEP（A1）
- 原 diff：`return (BigAutoField, SmallAutoField)` → `return (BigAutoField,)`
- 分类：🟢 不同节点（修改共享数据源 `_subclasses` 而非 golden 的 `__subclasscheck__`）。
- 理由：模拟开发者遗漏 `SmallAutoField` 的不完整编辑；`issubclass` 逻辑本身完好，
  表面正确，只在 Small 系列（含用户子类）检查时失效。
- 变异语义：`isinstance`/`issubclass` 对 SmallAutoField 全部返回 False，回退 super()。
- 验证：F2P FAILS（`test_isinstance_of_autofield[SmallAutoField]`、
  `test_issubclass_of_autofield[SmallAutoField/MySmallAutoField]` 等失败）。

### Group C —— KEEP（C1）
- 原 diff：`issubclass(subclass, self._subclasses)` → `isinstance(subclass, self._subclasses)`
- 分类：🟡 单 token 交换；但 M=1 → floor(1/2)=0，保留。
- 理由：与兄弟方法 `__instancecheck__` 中合法使用的 `isinstance` 视觉一致，极具迷惑性；
  对"类对象"参数 `isinstance` 恒为 False，使所有子类检查回退 super()，建模真实的
  类 vs 实例语义误用边界错误。
- 变异语义：对类参数恒 False，AutoField 子类检测全部失败。
- 验证：F2P FAILS（4 个 `test_issubclass_of_autofield` 子用例 + default_pk 相关）。

### Group D —— REPLACE（D1，原为 golden 逆向）
- 原 diff：恢复为 `subclass in self._subclasses`，是 golden 的精确反向 → 🔴。
- 最终 diff：
  ```python
  return issubclass(subclass, self._subclasses) and subclass.__module__ == BigAutoField.__module__ or super().__subclasscheck__(subclass)
  ```
- 变异语义：保留正确的 `issubclass` 形式，却附加一个伪装成"同包防御性检查"的
  模块来源守卫。内建 `BigAutoField`/`SmallAutoField`（同处 `django.db.models.fields`）
  通过，故仅用 stdlib 字段的测试全过；**仅其他模块中的用户自定义子类**被静默拒绝。
- 验证：F2P FAILS（`test_default_auto_field_setting_bigautofield_subclass` ERROR、
  `test_issubclass_of_autofield[MyBigAutoField/MySmallAutoField]` FAIL），所有 P2P 通过。

### Group E —— REPLACE（E1，原与 D 重复的 golden 逆向）
- 原 diff：与 D 字节级相同（`subclass in self._subclasses`），重复且为 golden 反向 → 🔴。
- 最终 diff：
  ```python
  return subclass.__name__ in ('BigAutoField', 'SmallAutoField') or super().__subclasscheck__(subclass)
  ```
- 变异语义：用脆弱的 `__name__` 字符串匹配替代结构化 `issubclass`，是常见但易错的重构。
  内建字段按名字匹配通过，典型测试全过；但任何 `__name__` 不同的用户子类被拒绝，
  以与 D 不同的机制复现原始 bug。
- 验证：F2P FAILS（同 D 的失败集合），P2P 全过。

## 新设计 Mutation 说明

D1 与 E1 构成正交失效模式，覆盖度互补：
- **D1（模块来源守卫）**：失败条件取决于子类**所在模块**，内建路径完全无感，
  攻击点是"跨模块用户子类"。
- **E1（名称字符串匹配）**：失败条件取决于子类**类名**，攻击点是"任何重命名/派生类"。
- 二者均通过典型只用内建字段的测试，仅在用户自定义子类场景失败，
  与 A1（删 SmallAutoField 分支）、C1（类/实例语义混淆）共同形成四种不同失效维度。

## 验证结果汇总

- 基线（golden + test_patch，无 mutation）：两模块共 **61 tests OK**（rc=0）。
- F2P 模块：`model_fields.test_autofield`、`model_options.test_default_pk`。
- 逐变异（从 JSONL diff 经 `git apply` 重放）：

| 组 | apply | py_compile | F2P 结果 | P2P |
|----|-------|-----------|----------|-----|
| A1 | rc=0 | OK | FAILS ✓ | 仅 Small 相关失败 |
| C1 | rc=0 | OK | FAILS ✓ | 仅 issubclass 路径失败 |
| D1 | rc=0 | OK | FAILS ✓ | 全过（仅用户子类用例失败） |
| E1 | rc=0 | OK | FAILS ✓ | 全过（仅用户子类用例失败） |
