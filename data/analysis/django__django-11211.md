# django__django-11211

## 问题背景

当使用 `GenericForeignKey (GFK)` 对 UUID 主键的模型进行 `prefetch_related` 时，预取结果无法正确关联到实例。原因：GFK 的 `object_pk` 字段（`CharField`）存储的是 UUID 的字符串表示，而被关联模型的 `pk` 是 `uuid.UUID` 对象。在 Python 中做关联匹配时，字符串与 UUID 对象不相等，导致预取到的对象无法被分配给对应实例（结果为 `None`）。

Golden patch 的修复：在 `UUIDField` 上添加 `get_prep_value` 方法，调用 `to_python` 将字符串 FK 值规范化为 `uuid.UUID` 对象，使两侧比较的类型一致。

## Golden Patch 语义分析

`get_prefetch_queryset` 返回两个 callable：
- `rel_obj_attr = lambda obj: (obj.pk, obj.__class__)` — 对已取回的关联对象，返回其 `uuid.UUID` 类型的 pk
- `instance_attr = gfk_key` — 对持有 GFK 的实例，调用 `model._meta.pk.get_prep_value(getattr(obj, self.fk_field))` 规范化存储在 `object_pk` 中的字符串值

在 `prefetch_one_level` 中，这两个 callable 的返回值用作字典键做匹配。如果 `gfk_key` 返回字符串而 `rel_obj_attr` 返回 UUID 对象，`dict.get(string_key)` 找不到 `UUID_key`，预取失败。

Golden patch 通过重写 `UUIDField.get_prep_value` 调用 `to_python`，确保 `gfk_key` 返回 `uuid.UUID` 对象，与 `obj.pk` 类型一致，匹配成功。

## 调用链分析

```
prefetch_related_objects()
  └─ prefetch_one_level(instances, GenericForeignKey_prefetcher, ...)
       ├─ GenericForeignKey.get_prefetch_queryset(instances)
       │    ├─ 收集 fk_val 到 fk_dict（原始字符串，如 "3c8e4c5a-..."）
       │    ├─ ct.get_all_objects_for_this_type(pk__in=fkeys)  ← DB查询
       │    ├─ rel_obj_attr = lambda obj: (obj.pk, obj.__class__)  ← 返回 uuid.UUID
       │    └─ instance_attr = gfk_key  ← 调用 UUIDField.get_prep_value
       │         └─ UUIDField.get_prep_value(str_val)
       │              └─ UUIDField.to_python(str_val)  → uuid.UUID
       ├─ 构建 rel_obj_cache: {(uuid.UUID, Article): [article_obj]}
       └─ 遍历 instances: instance_attr_val = gfk_key(obj) → (uuid.UUID, Article)
            → rel_obj_cache.get((uuid.UUID, Article)) = [article_obj] ✓
```

关键文件：
- `django/db/models/fields/__init__.py` — `UUIDField.get_prep_value`（golden patch 修改位置）
- `django/contrib/contenttypes/fields.py` — `GenericForeignKey.get_prefetch_queryset`（调用侧）
- `django/db/models/query.py` — `prefetch_one_level`（框架层匹配逻辑）

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 缺失 | 新设计 | mutations.jsonl 中无 A 组，需补充 |
| B | 缺失 | 新设计 | mutations.jsonl 中无 B 组，需补充 |
| C | 🔴 必须替换 | 替换 | `return value` 是 golden patch 的直接逆操作，等同还原 bug |
| D | 🔴 必须替换 | 替换 | 与 C 完全相同的 diff，重复冗余 |
| E | 🔴 必须替换 | 替换 | `isinstance(value, str)` 短路等同还原 fix，且有人工痕迹 |

语义浅层共 0 个（C、D 为直接还原，E 为功能等价还原）。

## 各组 Mutation 分析

### Group A — 替换（新设计）

**原 mutation**：（缺失，无原始 diff）

**分类**：新设计（跨文件，调用侧 bug）

**理由**：mutations.jsonl 中无 A 组，需设计高质量 mutation。选择在调用侧 `gfk_key` 中直接读取原始属性值而不调用 `get_prep_value`，模拟"开发者认为无需规范化 FK 值"的错误。位置在 `contenttypes/fields.py`，与 golden patch 所在文件不同，体现跨文件变异。

**最终 mutation**：
```diff
diff --git a/django/contrib/contenttypes/fields.py b/django/contrib/contenttypes/fields.py
index ed98ecb48c..21511d91e9 100644
--- a/django/contrib/contenttypes/fields.py
+++ b/django/contrib/contenttypes/fields.py
@@ -202,7 +202,7 @@ class GenericForeignKey(FieldCacheMixin):
             else:
                 model = self.get_content_type(id=ct_id,
                                               using=obj._state.db).model_class()
-                return (model._meta.pk.get_prep_value(getattr(obj, self.fk_field)),
+                return (getattr(obj, self.fk_field),
                         model)
 
         return (
```

**变异语义**：`gfk_key` 不再调用 `get_prep_value`，直接返回 `object_pk` 中存储的原始字符串。`rel_obj_attr` 仍返回 `uuid.UUID` 对象，二者类型不同，`rel_obj_cache.get(...)` 找不到匹配，`content_object_uuid` 结果为 `None`。对非 UUID 主键（整数 pk）不影响，因为整数主键的 `get_prep_value` 也返回整数。审查者看到的代码逻辑简洁，像是"直接读值"，不易察觉类型不一致问题。

---

### Group B — 替换（新设计）

**原 mutation**：（缺失，无原始 diff）

**分类**：新设计（UUIDField.to_python 条件变异）

**理由**：mutations.jsonl 中无 B 组，需设计高质量 mutation。在 `to_python` 中扩展 isinstance 守卫，将 `str` 排除在转换之外，模拟"字符串形式的 UUID 已经是合法表示，无需转换"的误判。此变异改变了 `to_python` 的语义契约，影响所有通过 `get_prep_value → to_python` 路径的调用。

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index 0d8612532d..f30031b7ff 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -2340,7 +2340,7 @@ class UUIDField(Field):
         return value.hex
 
     def to_python(self, value):
-        if value is not None and not isinstance(value, uuid.UUID):
+        if value is not None and not isinstance(value, (uuid.UUID, str)):
             input_form = 'int' if isinstance(value, int) else 'hex'
             try:
                 return uuid.UUID(**{input_form: value})
```

**变异语义**：`to_python` 对字符串值直接返回原字符串，不转换为 `uuid.UUID`。`get_prep_value` 因此对字符串 FK 也返回字符串，破坏预取匹配。同时影响 `get_db_prep_value` 中对字符串 value 的处理（但由于 `get_db_prep_value` 用 `not isinstance(value, uuid.UUID)` 检查，字符串仍会走 `to_python` 路径，造成自循环？不会，因为 `to_python` 对字符串直接返回，`get_db_prep_value` 拿到字符串后会调 `.hex` 报错）。实际影响：F2P 测试（prefetch GFK UUID pk）失败；基本的 UUID 对象操作通过；只有字符串 UUID 作为输入时才暴露 bug，难以通过简单测试发现。

---

### Group C — 替换

**原 mutation**：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -2327,7 +2327,7 @@ class UUIDField(Field):
 
     def get_prep_value(self, value):
         value = super().get_prep_value(value)
-        return self.to_python(value)
+        return value
```

**分类**：🔴 必须替换 — golden patch 的直接逆操作

**理由**：`return value` 等于把 `get_prep_value` 的行为还原为基类的行为（不做类型转换），与"没有 `get_prep_value` 方法"效果相同。这是对 golden fix 的精确逆操作。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index 0d8612532d..9724f09de5 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -2327,6 +2327,8 @@ class UUIDField(Field):
 
     def get_prep_value(self, value):
         value = super().get_prep_value(value)
+        if not isinstance(value, uuid.UUID):
+            return value
         return self.to_python(value)
 
     def get_db_prep_value(self, value, connection, prepared=False):
```

**变异语义**：添加了反向守卫——只有当 `value` 已经是 `uuid.UUID` 时才调用 `to_python`（但 `to_python` 对 UUID 直接返回，无效操作）；对字符串和整数直接返回原始值，不做转换。逻辑看起来像"已经是 UUID 对象的不需要处理，其他类型也不需要"，实际上是条件写反了。代码审查时不易发现，因为有 isinstance 检查看起来很"防御性"。F2P 测试中字符串 FK 无法匹配 UUID pk，测试失败。

---

### Group D — 替换

**原 mutation**：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -2327,7 +2327,7 @@ class UUIDField(Field):
 
     def get_prep_value(self, value):
         value = super().get_prep_value(value)
-        return self.to_python(value)
+        return value
```

**分类**：🔴 必须替换 — 与 Group C 完全相同的 diff，重复冗余

**理由**：与 C 组 diff 字节级别完全相同，两个 mutation 在5组中不应重复。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/django/contrib/contenttypes/fields.py b/django/contrib/contenttypes/fields.py
index ed98ecb48c..e2ab832e37 100644
--- a/django/contrib/contenttypes/fields.py
+++ b/django/contrib/contenttypes/fields.py
@@ -202,7 +202,7 @@ class GenericForeignKey(FieldCacheMixin):
             else:
                 model = self.get_content_type(id=ct_id,
                                               using=obj._state.db).model_class()
-                return (model._meta.pk.get_prep_value(getattr(obj, self.fk_field)),
+                return (str(model._meta.pk.get_prep_value(getattr(obj, self.fk_field))),
                         model)
 
         return (
```

**变异语义**：`gfk_key` 对 `get_prep_value` 的返回值调用 `str()`，将 UUID 对象转为字符串（如 `"3c8e4c5a-1234-..."`）。而 `rel_obj_attr` 返回原始 `uuid.UUID` 对象。`str(uuid.UUID(...)) != uuid.UUID(...)` 导致匹配失败。这是一个多文件、多层级的 bug：`get_prep_value` 在 `fields/__init__.py` 中正确返回 UUID，但在 `fields.py` 调用侧被额外调用了 `str()`。开发者可能认为"字符串化保持一致性"，代码审查不易察觉。

---

### Group E — 替换

**原 mutation**：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -2327,6 +2327,8 @@ class UUIDField(Field):
 
     def get_prep_value(self, value):
         value = super().get_prep_value(value)
+        if isinstance(value, str):
+            return value
         return self.to_python(value)
```

**分类**：🔴 必须替换 — 功能等价还原，且存在人工痕迹

**理由**：对于 GFK prefetch 场景，`fk_field` 存储的值总是字符串（`CharField`），所以 `isinstance(value, str)` 始终为 True，这个 `if` 分支总会被命中，导致 `to_python` 永远不被调用。功能上等价于直接 `return value`。开发者看到"对字符串做早期返回"这种模式很容易识破。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index 0d8612532d..b1f99e92c5 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -2327,7 +2327,8 @@ class UUIDField(Field):
 
     def get_prep_value(self, value):
         value = super().get_prep_value(value)
-        return self.to_python(value)
+        result = self.to_python(value)
+        return result.hex if isinstance(result, uuid.UUID) else result
```

**变异语义**：`get_prep_value` 正确调用 `to_python` 将字符串转为 UUID，但随后将 UUID 转为 hex 字符串（32位无连字符格式，如 `"3c8e4c5a1234..."`）。`rel_obj_attr` 返回 `uuid.UUID` 对象，`gfk_key` 返回 hex 字符串，类型和值格式均不同（即使 `str(uuid.UUID(...))` 带连字符，`.hex` 不带）。bug 精妙：代码逻辑流正确（调用了 `to_python`），只是最后一步做了额外的格式转换。模拟开发者认为"DB prep 应该返回 hex 格式"的误解。

## 新设计 Mutation 说明

### A — contenttypes/fields.py 调用侧省略规范化

基于分析：`gfk_key` 调用 `model._meta.pk.get_prep_value()` 是为了让 FK 字符串值与 `obj.pk` 的类型一致。删除这个调用，直接用 `getattr(obj, self.fk_field)` 返回原始字符串，在类型不匹配的字段（如 UUID）上会导致匹配失败。这模拟了"优化掉多余调用"的真实错误，且修改在不同文件（contenttypes/fields.py vs fields/__init__.py）。

### B — to_python 守卫条件扩展

基于分析：`to_python` 的语义是"将任意形式的输入转为标准 uuid.UUID 对象"。将 `str` 加入 isinstance 白名单，模拟"字符串 UUID 已经是可用形式"的误解。由于 GFK 的 `object_pk` 始终存储字符串，此 bug 精确命中测试场景。不影响直接赋值 UUID 对象的常规操作。

### C — get_prep_value 条件反转

基于分析：golden patch 添加的 `get_prep_value` 应对非 UUID 值调用 `to_python`。这里将条件写反（`if not isinstance(value, uuid.UUID): return value`），形成"对 UUID 以外的类型直接返回"的错误保护，使得字符串 FK 无法被转换。代码风格与"守卫条件"惯用法一致，不易发现。

### D — gfk_key 对结果调用 str()

基于分析：`get_prep_value` 正确返回 UUID 对象后，调用侧额外调用 `str()` 做格式统一。字符串化的 UUID（带连字符）与 `obj.pk`（UUID 对象）不相等，匹配失败。这是多文件协作中"最后一公里"类型不一致的典型错误。

### E — get_prep_value 返回 hex 格式

基于分析：`to_python` 正确将字符串转为 UUID，但随即调用 `.hex` 返回 32 位 hex 字符串。这破坏了 `get_prep_value` 的语义契约（应返回 Python 可比较对象，不是 DB 格式字符串）。开发者可能混淆了 `get_prep_value`（Python 层规范化）和 `get_db_prep_value`（DB 层格式化）的职责。
