# django__django-13410

## 问题背景

`django/core/files/locks.py` 的 POSIX 实现（`fcntl` 分支）中，`lock()` 和 `unlock()` 使用了错误的返回值逻辑：

```python
ret = fcntl.flock(_fd(f), flags)
return ret == 0
```

`fcntl.flock()` 在成功时返回 `None`（不是 `0`），失败时抛出 `OSError`（不是返回非零值）。所以 `ret == 0` 永远是 `False`（`None == 0` 为 `False`），导致这两个函数始终报告失败，即使操作成功。

修复：将 `lock()` 改为 try/except 结构（成功返回 `True`，捕获 `BlockingIOError` 返回 `False`），将 `unlock()` 改为直接调用后返回 `True`（unlock 本身不会失败）。

## Golden Patch 语义分析

**修复 `lock()`**：
```python
try:
    fcntl.flock(_fd(f), flags)
    return True          # 成功获取锁
except BlockingIOError:
    return False         # 非阻塞模式下无法立即获取锁
```

**修复 `unlock()`**：
```python
fcntl.flock(_fd(f), fcntl.LOCK_UN)
return True              # unlock 总是成功（或抛出，不处理）
```

注意：`unlock()` 不需要 try/except，因为解锁操作通常不会因为"锁被占用"而失败（`LOCK_NB` 仅对获取锁有意义）。

## 调用链分析

```
locks.lock(f1, LOCK_EX)
  → fcntl.flock(fd, LOCK_EX)  # 成功，返回 None
  → return True

locks.lock(f2, LOCK_EX | LOCK_NB)
  → fcntl.flock(fd, LOCK_EX | LOCK_NB)  # f1 已锁，抛出 BlockingIOError
  → except BlockingIOError: return False

locks.unlock(f1)
  → fcntl.flock(fd, LOCK_UN)  # 成功，返回 None
  → return True
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 缺失 | 新建 | 数据集无 A 组，设计 unlock() 返回 False |
| B | 语义浅层 | 保留 | 同时反转 lock()/unlock() 的返回值，位于关键逻辑 |
| C | 高质量 | 保留 | 恢复 ret == 0 原始 bug，C1 类型错误的类型强制 |
| D | 高质量 | 保留 | 删除 try/except，非阻塞失败时抛出异常而非返回 False |
| E | 高质量 | 保留 | except ValueError 捕获错误异常类型，BlockingIOError 未被捕获 |

语义浅层 1 个（B），保留。A 新建。

## 各组 Mutation 分析

### Group A — 新建

**最终 mutation**：
```diff
         def unlock(f):
             fcntl.flock(_fd(f), fcntl.LOCK_UN)
-            return True
+            return False
```
**变异语义**：`unlock()` 解锁后返回 `False` 而非 `True`。F2P 测试中 `assertIs(locks.unlock(f1), True)` 失败。这模拟了开发者混淆 POSIX 返回值约定：`False` 可能被认为表示"无错误"（类似 C 语言中 `0` 表示成功），而 Django locks API 约定 `True` 表示成功。

---

### Group B — 保留

**原 mutation**：
```diff
     try:
         fcntl.flock(_fd(f), flags)
-        return True
-    except BlockingIOError:
         return False
+    except BlockingIOError:
+        return True

     def unlock(f):
         fcntl.flock(_fd(f), fcntl.LOCK_UN)
-        return True
+        return False
```
**分类**：🟡 语义浅层（保留）
**理由**：修改位置处于 `lock()` 的成功/失败返回值和 `unlock()` 的返回值，这三处正是修复所针对的核心位置。保留原因：虽然是布尔值交换，但准确模拟了"成功报失败，失败报成功"的逻辑混淆，测试中的多个 `assertIs(..., True)` 和 `assertIs(..., False)` 都会失败。

---

### Group C — 保留

**原 mutation**：
```diff
-            try:
-                fcntl.flock(_fd(f), flags)
-                return True
-            except BlockingIOError:
-                return False
+            ret = fcntl.flock(_fd(f), flags)
+            return ret == 0
```
**分类**：🟢 保留
**理由**：恢复了原始 buggy 代码（`ret == 0`）。`fcntl.flock()` 返回 `None`，`None == 0` 为 `False`，所以 `lock()` 永远返回 `False`。这是对原始 bug 的精确还原，自然且隐蔽：对不了解 Python `fcntl` 返回 None 的开发者来说，`ret == 0` 看起来很合理（参考 C 语言约定）。

---

### Group D — 保留

**原 mutation**：
```diff
-            try:
-                fcntl.flock(_fd(f), flags)
-                return True
-            except BlockingIOError:
-                return False
+            fcntl.flock(_fd(f), flags)
+            return True
```
**分类**：🟢 保留
**理由**：删除了 try/except，只保留成功路径。当非阻塞锁失败时（`LOCK_NB` 模式），`BlockingIOError` 不被捕获，直接向上传播。F2P 测试 `test_exclusive_lock` 中 `locks.lock(f2, LOCK_EX | LOCK_NB)` 预期返回 `False`，但实际抛出异常，测试失败。开发者可能认为调用者应该自己处理异常，或认为 unlock 模式下不需要 try/except。

---

### Group E — 保留

**原 mutation**：
```diff
-            except BlockingIOError:
+            except ValueError:
                 return False
```
**分类**：🟢 保留
**理由**：A3 策略（替换异常类型）。`BlockingIOError` 是 `OSError` 的子类，是 `fcntl.flock()` 在非阻塞获取失败时抛出的异常。`ValueError` 捕获的是完全不同类型的错误（参数值无效）。当非阻塞锁失败时，`BlockingIOError` 不被 `except ValueError` 捕获，向上传播，F2P 测试失败。代码审查中难以发现：两者都是具体的异常类名，不熟悉 `fcntl` 的开发者不会注意到差异。

## 新设计 Mutation 说明

### Group A（A1 — 返回值约定错误）
`unlock()` 调用 `fcntl.flock(LOCK_UN)` 后应返回 `True` 表示成功。将其改为 `False` 模拟了开发者混淆 API 成功信号的错误：在一些传统约定中（C 语言、shell exit code），`0/False` 表示成功，而 Django locks API 明确使用 `True` 表示成功。这类约定混淆在跨语言背景的开发者中很常见。
