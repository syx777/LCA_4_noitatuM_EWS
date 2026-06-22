# django__django-13821

## 问题背景

Django 决定放弃对 SQLite < 3.9.0 的支持（表达式索引和 `SQLITE_ENABLE_JSON1` 编译选项需要 3.9.0+）。Golden patch 把 `check_sqlite_version()` 中的最低版本门槛从 `(3, 8, 3)` 提升到 `(3, 9, 0)`，并同步更新报错信息。该函数在 `django/db/backends/sqlite3/base.py` 模块加载时（`check_sqlite_version()` 顶层调用）执行一次：若运行环境的 SQLite 版本过低，则抛出 `ImproperlyConfigured`。

## Golden Patch 语义分析

```python
def check_sqlite_version():
    if Database.sqlite_version_info < (3, 9, 0):
        raise ImproperlyConfigured(
            'SQLite 3.9.0 or later is required (found %s).' % Database.sqlite_version
        )
```

核心语义有两层：
1. **门槛值**：比较 `Database.sqlite_version_info`（一个版本号元组，如 `(3, 8, 11, 1)`）是否小于 `(3, 9, 0)`。元组比较是逐元素字典序，因此 `(3, 8, 11, 1) < (3, 9, 0)` 为真。
2. **错误信息契约**：抛出的异常必须是 `ImproperlyConfigured`，且消息字符串必须精确等于 `'SQLite 3.9.0 or later is required (found <实际版本>).'`。

F2P 测试 `test_check_sqlite_version` 用 `mock.patch.object` 把 `dbapi2.sqlite_version_info` 伪造成 `(3, 8, 11, 1)`、`sqlite_version` 伪造成 `'3.8.11.1'`，并用 `assertRaisesMessage` 断言抛出的消息精确为 `'SQLite 3.9.0 or later is required (found 3.8.11.1).'`。

因此任何 mutation 只要满足下列之一即可让 F2P 失败：
- 对版本 `(3, 8, 11, 1)` **不再抛出**异常；
- 抛出了**错误类型或错误消息**的异常。

## 调用链分析

- `check_sqlite_version()` 在模块顶层被直接调用一次（导入 `django.db.backends.sqlite3.base` 时触发），用真实环境版本做一次性校验。
- 测试通过 `from django.db.backends.sqlite3.base import check_sqlite_version` 拿到函数引用，再用 mock 注入低版本后手动调用。
- 关键点：`Database` 即 `sqlite3.dbapi2`，所以 mock `dbapi2.sqlite_version_info` 能直接改变函数内读取的值。模块顶层那次真实调用（运行环境 SQLite 3.45）总是通过、不抛异常——这一点被 Group D 利用。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原 mutation 把门槛改 `(3,8,0)` 但消息仍硬编码 "3.9.0"，代码与消息矛盾，审查必现 |
| B | 🔴 必须替换 | 替换 | 原 mutation 把整个校验改为 `pass`，等同删除函数体，是明显的功能阉割 |
| C | 🟢 保留 | 保留 | 把元组比较改成只看主版本号 `[0] < 3`，形态自然，难以一眼看出门槛被悄悄取消 |
| D | 🟢 保留 | 保留 | 引入模块级 `_version_check_done` 幂等开关，借助导入时首次调用消耗掉检查，跨调用状态污染，极隐蔽 |
| E | 🟢 保留 | 保留 | 增加 `strict=False` 参数门控，把强制校验变成可选，调用者不传参即失效 |

语义浅层共 0 个；必须替换 2 个（A、B），均替换为高质量变异。C/D/E 全部保留。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：
```diff
-    if Database.sqlite_version_info < (3, 9, 0):
+    if Database.sqlite_version_info < (3, 8, 0):
         raise ImproperlyConfigured(
             'SQLite 3.9.0 or later is required (found %s).' % Database.sqlite_version
```
**分类**：🔴 必须替换
**理由**：门槛被改成 `(3, 8, 0)`，但下一行的报错信息仍硬编码 `'SQLite 3.9.0 or later is required'`。代码逻辑（要求 3.8.0）与文案（声称要求 3.9.0）自相矛盾，任何审查者扫一眼就会发现，属于不自然的人工痕迹。

**最终 mutation**：
```diff
-def check_sqlite_version():
-    if Database.sqlite_version_info < (3, 9, 0):
+def check_sqlite_version(min_version=(3, 8, 0)):
+    if Database.sqlite_version_info < min_version:
         raise ImproperlyConfigured(
-            'SQLite 3.9.0 or later is required (found %s).' % Database.sqlite_version
+            'SQLite %s or later is required (found %s).'
+            % ('.'.join(map(str, min_version)), Database.sqlite_version)
         )
```
**变异语义**：把硬编码门槛重构成带默认值的参数 `min_version`，并让报错信息**根据 `min_version` 动态生成**，从而消除原 mutation 的代码/文案矛盾。表面看是一次合理的"让版本可配置"的重构，代码风格自洽、消息随门槛联动；但默认门槛被悄悄设为 `(3, 8, 0)`，比正确的 `(3, 9, 0)` 低一个次版本号。对 `(3, 8, 11, 1)` 不再抛异常，F2P 失败。由于消息现在动态拼出 `'SQLite 3.8.0 or later is required...'`，不再出现 hardcode 矛盾，逐行审查也只会觉得是个无害的默认值。属于 A1（修改参数默认值/语义）。

### Group B — 替换
**原 mutation**：
```diff
 def check_sqlite_version():
-    if Database.sqlite_version_info < (3, 9, 0):
-        raise ImproperlyConfigured(
-            'SQLite 3.9.0 or later is required (found %s).' % Database.sqlite_version
-        )
+    pass
```
**分类**：🔴 必须替换
**理由**：整个校验体被替换为 `pass`，函数完全空转。这等同于直接删除版本检查，是显而易见的功能阉割，审查中立刻暴露。

**最终 mutation**：
```diff
 def check_sqlite_version():
-    if Database.sqlite_version_info < (3, 9, 0):
+    major, minor = Database.sqlite_version_info[:2]
+    if major < 3 or (major == 3 and minor < 8):
         raise ImproperlyConfigured(
             'SQLite 3.9.0 or later is required (found %s).' % Database.sqlite_version
         )
```
**变异语义**：把元组整体比较改写成"手动拆主次版本号再判断"的等价形式——这种写法在真实代码里很常见，看起来更显式、更可读。但边界条件写成 `minor < 8`（应为 `minor < 9`），引入了一个次版本号上的 off-by-one。对 `(3, 8, 11, 1)`：`major=3, minor=8`，`3<3 or (3==3 and 8<8)` 为 `False`，不抛异常，F2P 失败。由于消息保持不变、逻辑结构看似合理，审查者很容易接受这个"重构"而忽略 `8` 与 `9` 的差异。属于 B1（off-by-one 边界错误）。

### Group C — 保留
**原 mutation**：
```diff
-    if Database.sqlite_version_info < (3, 9, 0):
+    if Database.sqlite_version_info[0] < 3:
```
**分类**：🟢 保留
**理由**：把"元组与元组比较"改成"只取主版本号 `[0]` 与标量 `3` 比较"，改变了比较的数据形态。门槛实际退化为"只要主版本号 ≥ 3 就放行"，3.x 的所有次版本都通过。形态自然（看起来像简化判断），不在 golden 的语义焦点上明示门槛被取消，难以浅层检测。属于 C1（破坏隐式类型/形态约定）。

### Group D — 保留
**原 mutation**：
```diff
+_version_check_done = False
+
 def check_sqlite_version():
+    global _version_check_done
+    if _version_check_done:
+        return  # Skip check on subsequent calls - breaks idempotency
+    _version_check_done = True
     if Database.sqlite_version_info < (3, 9, 0):
```
**分类**：🟢 保留
**理由**：引入模块级布尔开关，让 `check_sqlite_version()` 只在首次调用时真正校验。由于模块导入时顶层那次调用（真实环境 SQLite 3.45，通过）已经把 `_version_check_done` 置为 `True`，测试随后再调用时直接 `return`，对伪造的低版本不再校验。bug 的根因（导入时的副作用）与表现（测试调用失效）分离在不同调用之间，是典型的状态污染/幂等性破坏，极难通过单点阅读发现。属于 D2（破坏方法幂等性）。

### Group E — 保留
**原 mutation**：
```diff
-def check_sqlite_version():
-    if Database.sqlite_version_info < (3, 9, 0):
+def check_sqlite_version(strict=False):
+    if strict and Database.sqlite_version_info < (3, 9, 0):
```
**分类**：🟢 保留
**理由**：把强制校验改成由新参数 `strict` 门控，默认 `False`。所有不带参数的现有调用（包括模块顶层调用和测试调用）都走"非严格"分支，校验被静默跳过。看似是一次合理的"提供宽松模式"的接口增强，但默认行为已从"总是校验"变成"默认不校验"，破坏了调用者的隐式假设。属于 E2（隐式行为变为显式参数门控）。

## 新设计 Mutation 说明

- **Group A（A1）**：基于对"代码与报错文案必须一致"这一审查痛点的理解，将原 mutation 的硬编码矛盾消除——通过把门槛参数化并让消息动态生成，使变异后的代码完全自洽。真实开发者在"让版本要求可配置"时确实可能把默认值写错一个次版本号，属于高度可信的人为失误。
- **Group B（B1）**：基于"元组比较常被手动展开为逐位判断"的常见重构模式，引入一个隐藏在 `major/minor` 拆解逻辑里的 off-by-one（`minor < 8` 而非 `< 9`）。这种写法比原 `pass` 自然得多，消息保持正确，审查者关注的是"重构是否等价"而非具体边界数字，容易放过。
- 两个新变异都只修改 `django/db/backends/sqlite3/base.py`（允许的源文件），不触碰测试文件；均已通过 Step 5 自查（patch 后可应用、`py_compile` 通过、模拟 F2P 测试确认对 `(3,8,11,1)` 不再抛出预期异常，测试失败）。
