# django__django-13343

## 问题背景

`FileField` 支持 `storage` 参数为 callable（在运行时动态选择存储后端）。但在 `deconstruct()` 方法中，代码直接将 `self.storage`（已求值的 Storage 实例）写入 kwargs，而非原始 callable。这导致 `makemigrations` 时将具体的 Storage 实例硬编码进迁移文件，破坏了"可在不同环境使用不同 storage"的语义承诺。

## Golden Patch 语义分析

修复分两处：

1. **`__init__` 中保存 callable 引用**：在 `if callable(self.storage):` 块内，先执行 `self._storage_callable = self.storage`（保存原始 callable），再执行 `self.storage = self.storage()`（求值）。关键：必须在求值**之前**保存，否则保存的是 Storage 实例。

2. **`deconstruct()` 中返回 callable**：将 `kwargs['storage'] = self.storage` 改为 `kwargs['storage'] = getattr(self, '_storage_callable', self.storage)`。`getattr` 的默认值语义：若字段是通过 callable storage 初始化的，返回 callable；否则返回已有 Storage 实例。

## 调用链分析

```
FileField.__init__(storage=callable_storage)
  └── self.storage = storage or default_storage   # = callable_storage (function)
  └── if callable(self.storage):
        self._storage_callable = self.storage     # ← 保存 callable
        self.storage = self.storage()             # ← 求值为 temp_storage 实例
        isinstance check...

FileField.deconstruct()
  └── super().deconstruct()  # Field.deconstruct()
  └── if self.storage is not default_storage:
        kwargs['storage'] = getattr(self, '_storage_callable', self.storage)
        # 有 callable → 返回 callable_storage 函数
        # 无 callable → 返回 Storage 实例
  └── return name, path, args, kwargs

makemigrations → 调用 deconstruct() → 序列化 kwargs['storage']
  # 正确：序列化为 callable 引用
  # 错误：序列化为具体 Storage 实例
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 缺失 | 新增 | mutations.jsonl 中无 Group A |
| B | 语义浅层（B3） | 保留 | `is not` → `is`，修改位置关键，模拟真实的判断方向失误 |
| C | 🔴 必须替换 | 替换 | `getattr(self, '_storage_callable', ...)` → `self.storage`，直接还原 deconstruct 修复 |
| D | 🔴 必须替换 | 替换 | 用注释注掉 `_storage_callable = self.storage`，不自然且功能等同还原 |
| E | 🔴 必须替换 | 替换 | 同 C 但加注释，功能等同 C + 明显人工痕迹 |

语义浅层共 1 个（B），floor(1/2)=0 个需替换，保留 B。

## 各组 Mutation 分析

### Group A — 新增

**原 mutation**：（缺失）

**分类**：新增

**最终 mutation**：

```diff
diff --git a/django/db/models/fields/files.py b/django/db/models/fields/files.py
index db2450a738..a2dff87904 100644
--- a/django/db/models/fields/files.py
+++ b/django/db/models/fields/files.py
@@ -230,8 +230,8 @@ class FileField(Field):
         self.storage = storage or default_storage
         if callable(self.storage):
             # Hold a reference to the callable for deconstruct().
-            self._storage_callable = self.storage
             self.storage = self.storage()
+            self._storage_callable = self.storage
             if not isinstance(self.storage, Storage):
                 raise TypeError(
                     "%s.storage must be a subclass/instance of %s.%s"
```

**变异语义**：交换 `self._storage_callable = self.storage` 和 `self.storage = self.storage()` 的顺序——先求值，再保存。此时 `_storage_callable` 存储的是已求值的 Storage 实例（`temp_storage`），而非原始 callable。`deconstruct()` 中 `getattr(self, '_storage_callable', ...)` 找到该属性并返回 `temp_storage`，而非 `callable_storage` 函数。F2P 测试 `assertIs(storage, callable_storage)` 失败。难以发现：代码行内容完全相同，仅顺序颠倒，且注释没有随之更新暗示问题。

---

### Group B — 保留

**原 mutation**：

```diff
diff --git a/django/db/models/fields/files.py b/django/db/models/fields/files.py
index db2450a738..c9c3ba91e7 100644
--- a/django/db/models/fields/files.py
+++ b/django/db/models/fields/files.py
@@ -280,7 +280,7 @@ class FileField(Field):
         if kwargs.get("max_length") == 100:
             del kwargs["max_length"]
         kwargs['upload_to'] = self.upload_to
-        if self.storage is not default_storage:
+        if self.storage is default_storage:
             kwargs['storage'] = getattr(self, '_storage_callable', self.storage)
         return name, path, args, kwargs
```

**分类**：🟡 语义浅层（B3）— 保留

**理由**：`is not → is` 将"非默认存储时包含 storage 参数"反转为"是默认存储时才包含"。由于通常 storage 不是默认值（callable storage 求值后得到 temp_storage，不是 default_storage），这个条件通常为 False，导致 `kwargs` 中根本没有 `storage` key，deconstruct 输出中缺少 storage 参数。修改位置在关键的条件控制处，模拟了开发者对 `is not` 方向的判断错误。

**变异语义**：`deconstruct()` 对所有非默认存储的字段不输出 `storage` key，对 default_storage 字段反而输出。F2P 测试 `kwargs['storage']` → KeyError 或得到 default_storage 而非 callable_storage。

---

### Group C — 替换

**原 mutation**：

```diff
diff --git a/django/db/models/fields/files.py b/django/db/models/fields/files.py
index db2450a738..c6f11e6b84 100644
--- a/django/db/models/fields/files.py
+++ b/django/db/models/fields/files.py
@@ -281,7 +281,7 @@ class FileField(Field):
             del kwargs["max_length"]
         kwargs['upload_to'] = self.upload_to
         if self.storage is not default_storage:
-            kwargs['storage'] = getattr(self, '_storage_callable', self.storage)
+            kwargs['storage'] = self.storage
         return name, path, args, kwargs
```

**分类**：🔴 必须替换 — 直接还原 deconstruct 修复，功能等价于 base_commit 原始代码

**最终 mutation**：

```diff
diff --git a/django/db/models/fields/files.py b/django/db/models/fields/files.py
index db2450a738..ab7be01b35 100644
--- a/django/db/models/fields/files.py
+++ b/django/db/models/fields/files.py
@@ -281,7 +281,7 @@ class FileField(Field):
             del kwargs["max_length"]
         kwargs['upload_to'] = self.upload_to
         if self.storage is not default_storage:
-            kwargs['storage'] = getattr(self, '_storage_callable', self.storage)
+            kwargs['storage'] = getattr(self, '_callable_storage', self.storage)
         return name, path, args, kwargs
 
     def get_internal_type(self):
```

**变异语义**：将 `getattr` 的属性名从 `'_storage_callable'` 改为 `'_callable_storage'`（首尾单词顺序颠倒）。此属性从未被设置（`__init__` 中存储的是 `_storage_callable`），所以 `getattr` 始终返回默认值 `self.storage`（已求值的 Storage 实例）。`deconstruct()` 返回实例而非 callable。`*` vs `**` 同理，属性名换序几乎不可能被肉眼发现，代码结构完全正常。

---

### Group D — 替换

**原 mutation**：

```diff
diff --git a/django/db/models/fields/files.py b/django/db/models/fields/files.py
index db2450a738..1a3761fbce 100644
--- a/django/db/models/fields/files.py
+++ b/django/db/models/fields/files.py
@@ -230,7 +230,7 @@ class FileField(Field):
         self.storage = storage or default_storage
         if callable(self.storage):
             # Hold a reference to the callable for deconstruct().
-            self._storage_callable = self.storage
+            # self._storage_callable = self.storage
             self.storage = self.storage()
```

**分类**：🔴 必须替换 — 用注释注掉代码，明显人工痕迹

**最终 mutation**：

```diff
diff --git a/django/db/models/fields/files.py b/django/db/models/fields/files.py
index db2450a738..4ec161e417 100644
--- a/django/db/models/fields/files.py
+++ b/django/db/models/fields/files.py
@@ -232,6 +232,7 @@ class FileField(Field):
             # Hold a reference to the callable for deconstruct().
             self._storage_callable = self.storage
             self.storage = self.storage()
+            self._storage_callable = self.storage
             if not isinstance(self.storage, Storage):
                 raise TypeError(
                     "%s.storage must be a subclass/instance of %s.%s"
```

**变异语义**：在求值之后**再次**赋值 `self._storage_callable = self.storage`，覆盖之前保存的 callable 引用。第一次赋值（求值前）保存了 callable，第二次赋值（求值后）将其覆盖为 Storage 实例。最终 `_storage_callable` 持有实例而非 callable，`deconstruct()` 返回实例。此 mutation 是典型的"状态重置"错误（D1）——看起来像是"补充初始化"，但实际覆盖了有价值的状态。因为两行代码形式相同仅位置不同，极难在 code review 中发现。

---

### Group E — 替换

**原 mutation**：

```diff
diff --git a/django/db/models/fields/files.py b/django/db/models/fields/files.py
index db2450a738..e5311a5162 100644
--- a/django/db/models/fields/files.py
+++ b/django/db/models/fields/files.py
@@ -281,7 +281,8 @@ class FileField(Field):
             del kwargs["max_length"]
         kwargs['upload_to'] = self.upload_to
         if self.storage is not default_storage:
-            kwargs['storage'] = getattr(self, '_storage_callable', self.storage)
+            # By default, always use the evaluated storage, not the callable
+            kwargs['storage'] = self.storage
         return name, path, args, kwargs
```

**分类**：🔴 必须替换 — 功能等同原始 C mutation，加注释明显人工痕迹

**最终 mutation**：

```diff
diff --git a/django/db/models/fields/files.py b/django/db/models/fields/files.py
index db2450a738..1407ba3dd3 100644
--- a/django/db/models/fields/files.py
+++ b/django/db/models/fields/files.py
@@ -230,7 +230,8 @@ class FileField(Field):
         self.storage = storage or default_storage
         if callable(self.storage):
             # Hold a reference to the callable for deconstruct().
-            self._storage_callable = self.storage
+            if isinstance(self.storage, type):
+                self._storage_callable = self.storage
             self.storage = self.storage()
             if not isinstance(self.storage, Storage):
                 raise TypeError(
```

**变异语义**：只对类（`isinstance(self.storage, type)` 为 True）保存 `_storage_callable`，对普通函数（`isinstance → False`）不保存。`callable_storage` 是函数（非 type），因此 `_storage_callable` 不被设置。`deconstruct()` 中 `getattr(self, '_storage_callable', self.storage)` 回退到 `self.storage`（求值后的 `temp_storage` 实例）。F2P 测试 `assertIs(storage, callable_storage)` 失败，但测试 `storage_callable_class`（使用 `CallableStorage` 类）可能仍正常。此 mutation 利用了"函数与类都是 callable 但 isinstance(x, type) 不同"这一 Python 语义细节，极具迷惑性。

## 新设计 Mutation 说明

### Group A（A1 策略）
- **代码分析**：`_storage_callable` 存储的时机决定了它捕获的是 callable 还是实例。`self.storage = self.storage()` 一旦执行，`self.storage` 就变成了 Storage 实例，后续无法再获取原始 callable。
- **位置选择**：修改在 `__init__` 方法中，与其他 mutation 的修改位置（`deconstruct`）不同。
- **模拟的错误**：开发者可能认为"先求值，再记录备份"也是合理的初始化模式，错误交换了两行顺序。

### Group C（C1 策略）
- **代码分析**：`getattr` 的第二个参数是属性名字符串，必须与 `__init__` 中赋值时使用的名字完全一致。`_storage_callable` 与 `_callable_storage` 是两个不同的属性名。
- **位置选择**：修改在 `deconstruct()` 中，位置与 B 不同（B 改条件，C 改 getattr 名）。
- **模拟的错误**：属性名中单词顺序颠倒是常见的命名 typo，`storage_callable` 和 `callable_storage` 在语义上都说得通，非常难以发现。

### Group D（D1 策略）
- **代码分析**：`_storage_callable` 在 `__init__` 中被赋值两次——第一次（正确）保存 callable，第二次（错误）覆盖为求值后的实例。这是典型的"状态被后续操作覆盖"的初始化 bug。
- **位置选择**：在 `__init__` 中添加一行，而 Group A 是交换两行顺序，位置不同。
- **模拟的错误**：开发者在重构时"补充"了一行看似合理的赋值，未意识到覆盖了已有的正确值。

### Group E（E2 策略）
- **代码分析**：`callable()` 内置函数对函数和类都返回 True，而 `isinstance(x, type)` 只对类返回 True。将无条件保存改为按类型有条件保存，引入了对普通函数 callable 的处理缺陷。
- **位置选择**：修改在 `__init__` 的 callable 检查块内，通过添加 isinstance 子条件改变存储逻辑。
- **模拟的错误**：开发者可能认为只有"类"形式的 storage 才需要特殊处理（因为类实例化是"真正的构造"），错误地过滤掉了普通函数形式的 storage callable。
