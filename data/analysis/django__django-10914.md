# django__django-10914

## 问题背景

Django 的文件上传权限设置 `FILE_UPLOAD_PERMISSIONS` 默认为 `None`，导致上传文件的权限不一致：
- 使用 `MemoryUploadedFile`（小文件）时，通过 `os.open(..., 0o666)` 创建，实际权限受 umask 影响（通常为 0o644）
- 使用 `TemporaryUploadedFile`（大文件）时，通过 `tempfile.NamedTemporaryFile` + `os.rename` 实现，某些系统（如 CentOS 7 + Python 3.6）产生 0o600 权限

Golden patch 将 `FILE_UPLOAD_PERMISSIONS = None` 改为 `FILE_UPLOAD_PERMISSIONS = 0o644`，使所有上传文件都获得一致的 0o644 权限。

## Golden Patch 语义分析

**修复位置**：`django/conf/global_settings.py` 第 307 行。

**核心语义**：将 `FILE_UPLOAD_PERMISSIONS` 从"不设置权限"（None 表示不调用 `os.chmod`）改为"显式设置 0o644"。

在 `FileSystemStorage._save()` 中：
```python
if self.file_permissions_mode is not None:
    os.chmod(full_path, self.file_permissions_mode)
```
- `file_permissions_mode` 是 `cached_property`，读取 `settings.FILE_UPLOAD_PERMISSIONS`
- 修复前：`None` → 不执行 `os.chmod`，权限完全依赖 tempfile 或 umask
- 修复后：`0o644` → 始终 `os.chmod(full_path, 0o644)`，权限一致

**F2P 测试**（`test_override_file_upload_permissions`）：
```python
self.assertEqual(default_storage.file_permissions_mode, 0o644)  # 修复后断言
with self.settings(FILE_UPLOAD_PERMISSIONS=0o777):
    self.assertEqual(default_storage.file_permissions_mode, 0o777)
```
任何将 `FILE_UPLOAD_PERMISSIONS` 设置为非 `0o644` 的 mutation 都会导致第一个断言失败。

## 调用链分析

```
global_settings.py
  FILE_UPLOAD_PERMISSIONS = 0o644
    ↓ (settings 读取)
FileSystemStorage.file_permissions_mode (cached_property)
  = _value_or_setting(self._file_permissions_mode, settings.FILE_UPLOAD_PERMISSIONS)
    ↓
FileSystemStorage._save(name, content)
  if self.file_permissions_mode is not None:
      os.chmod(full_path, self.file_permissions_mode)
```

`_clear_cached_properties` 在 `setting_changed` 信号触发时清空 `file_permissions_mode` 缓存，使 `with self.settings(...)` 的上下文管理器能正确更新缓存值。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 语义浅层 | 保留 | `0o600` 是真实安全误解（仅 owner 读写），是边界错误典型 |
| B | 语义浅层 | 替换 | `0o643` 是不自然的值（execute bit 混入），真实开发者不会写出 |
| C | 语义浅层 | 保留 | `0o640` 组无写权限，模拟真实权限收紧的判断失误 |
| D | 必须替换 | 替换 | 注释掉该行 = 直接还原 base_commit 的 `None` 行为，等价于逆操作 |
| E | 语义浅层 | 替换 | `0o600` 与 Group A 完全相同，重复 mutation |

语义浅层共 4 个（A/B/C/E），替换其中最弱的 floor(4/2) = 2 个：B（不自然）和 E（重复）。
加上必须替换的 D，共替换 3 个：B、D、E。

## 各组 Mutation 分析

### Group A — 保留
**原 mutation**：
```diff
diff --git a/django/conf/global_settings.py b/django/conf/global_settings.py
index bdeec80610..c8493a4d7f 100644
--- a/django/conf/global_settings.py
+++ b/django/conf/global_settings.py
@@ -304,7 +304,7 @@ FILE_UPLOAD_TEMP_DIR = None
 
 # The numeric mode to set newly-uploaded files to. The value should be a mode
 # you'd pass directly to os.chmod; see https://docs.python.org/library/os.html#files-and-directories.
-FILE_UPLOAD_PERMISSIONS = 0o644
+FILE_UPLOAD_PERMISSIONS = 0o600
 
 # The numeri
```
**分类**：🟡 语义浅层（保留）
**理由**：`0o600` 是真实开发者在安全考量下可能选择的值（仅 owner 读写），模拟"过度收紧权限"的判断误差。位置在关键配置节点，简单测试无法与 `0o644` 区分（除非测试检查实际 chmod 值）。
**最终 mutation**（保留，与原相同）：
```diff
diff --git a/django/conf/global_settings.py b/django/conf/global_settings.py
index bdeec80610..c8493a4d7f 100644
--- a/django/conf/global_settings.py
+++ b/django/conf/global_settings.py
@@ -304,7 +304,7 @@ FILE_UPLOAD_TEMP_DIR = None
 
 # The numeric mode to set newly-uploaded files to. The value should be a mode
 # you'd pass directly to os.chmod; see https://docs.python.org/library/os.html#files-and-directories.
-FILE_UPLOAD_PERMISSIONS = 0o644
+FILE_UPLOAD_PERMISSIONS = 0o600
 
 # The numeri
```
**变异语义**：设置过于严格的权限（仅 owner r/w），导致其他用户（如 web server 进程）无法读取上传的文件。F2P 测试中 `assertEqual(file_permissions_mode, 0o644)` 会断言失败（0o600 ≠ 0o644）。

---

### Group B — 替换
**原 mutation**：
```diff
diff --git a/django/conf/global_settings.py b/django/conf/global_settings.py
index bdeec80610..b226358d4a 100644
--- a/django/conf/global_settings.py
+++ b/django/conf/global_settings.py
@@ -304,7 +304,7 @@ FILE_UPLOAD_TEMP_DIR = None
 
 # The numeric mode to set newly-uploaded files to. The value should be a mode
 # you'd pass directly to os.chmod; see https://docs.python.org/library/os.html#files-and-directories.
-FILE_UPLOAD_PERMISSIONS = 0o644
+FILE_UPLOAD_PERMISSIONS = 0o643
 
 # The numeri
```
**分类**：🟡 语义浅层（替换）
**理由**：`0o643` 在实际场景中极为罕见（group 无写但有执行，other 有读写但无执行），真实开发者不会选择这个值。代码审查者一眼就能看出异常。

**最终 mutation**（替换为新 diff）：
```diff
diff --git a/django/conf/global_settings.py b/django/conf/global_settings.py
index bdeec80610..d056c72e7d 100644
--- a/django/conf/global_settings.py
+++ b/django/conf/global_settings.py
@@ -304,7 +304,7 @@ FILE_UPLOAD_TEMP_DIR = None
 
 # The numeric mode to set newly-uploaded files to. The value should be a mode
 # you'd pass directly to os.chmod; see https://docs.python.org/library/os.html#files-and-directories.
-FILE_UPLOAD_PERMISSIONS = 0o644
+FILE_UPLOAD_PERMISSIONS = 0o664
 
 # The numeric mode to assign to newly-created directories, when uploading files.
 # The value should be a mode as you'd pass to os.chmod;
```
**变异语义**：设置 group-writable 权限（664）。开发者可能认为"文件应该对 web server 所在组可写"，从而误设组写权限。0o664 看起来与 0o644 非常接近（仅 group write bit 不同），代码审查很难发现。F2P 测试中 `assertEqual(file_permissions_mode, 0o644)` 会失败（0o664 ≠ 0o644）。

---

### Group C — 保留
**原 mutation**：
```diff
diff --git a/django/conf/global_settings.py b/django/conf/global_settings.py
index bdeec80610..580dece238 100644
--- a/django/conf/global_settings.py
+++ b/django/conf/global_settings.py
@@ -304,7 +304,7 @@ FILE_UPLOAD_TEMP_DIR = None
 
 # The numeric mode to set newly-uploaded files to. The value should be a mode
 # you'd pass directly to os.chmod; see https://docs.python.org/library/os.html#files-and-directories.
-FILE_UPLOAD_PERMISSIONS = 0o644
+FILE_UPLOAD_PERMISSIONS = 0o640
 
 # The numeri
```
**分类**：🟡 语义浅层（保留）
**理由**：`0o640` 是常见的生产环境收紧策略（组用户只读，其他用户无权限），模拟"安全加固时过度限制 other 用户"的真实误判。与 A (0o600) 修改位置相同但值不同，两者互补。
**最终 mutation**（保留，与原相同）：
```diff
diff --git a/django/conf/global_settings.py b/django/conf/global_settings.py
index bdeec80610..580dece238 100644
--- a/django/conf/global_settings.py
+++ b/django/conf/global_settings.py
@@ -304,7 +304,7 @@ FILE_UPLOAD_TEMP_DIR = None
 
 # The numeric mode to set newly-uploaded files to. The value should be a mode
 # you'd pass directly to os.chmod; see https://docs.python.org/library/os.html#files-and-directories.
-FILE_UPLOAD_PERMISSIONS = 0o644
+FILE_UPLOAD_PERMISSIONS = 0o640
 
 # The numeri
```
**变异语义**：过度限制 other 组权限，导致非 owner/group 用户（如 nginx、CDN 回源）无法读取文件。F2P 测试中 `assertEqual(file_permissions_mode, 0o644)` 会断言失败（0o640 ≠ 0o644）。

---

### Group D — 替换
**原 mutation**：
```diff
diff --git a/django/conf/global_settings.py b/django/conf/global_settings.py
index bdeec80610..8279024190 100644
--- a/django/conf/global_settings.py
+++ b/django/conf/global_settings.py
@@ -304,7 +304,7 @@ FILE_UPLOAD_TEMP_DIR = None
 
 # The numeric mode to set newly-uploaded files to. The value should be a mode
 # you'd pass directly to os.chmod; see https://docs.python.org/library/os.html#files-and-directories.
-FILE_UPLOAD_PERMISSIONS = 0o644
+# FILE_UPLOAD_PERMISSIONS = 0o644
 
 # The nume
```
**分类**：🔴 必须替换
**理由**：注释掉整行 = `FILE_UPLOAD_PERMISSIONS` 未定义，Python 会回退到 `None`（Django 的 `settings` 对象如果找不到该属性就会使用全局默认）。这等价于直接还原到 base_commit 的原始行为（`FILE_UPLOAD_PERMISSIONS = None`）。此外，注释手法明显是人工操作痕迹，不自然。

**最终 mutation**（替换为新 diff）：
```diff
diff --git a/django/conf/global_settings.py b/django/conf/global_settings.py
index bdeec80610..0f0044984d 100644
--- a/django/conf/global_settings.py
+++ b/django/conf/global_settings.py
@@ -304,7 +304,7 @@ FILE_UPLOAD_TEMP_DIR = None
 
 # The numeric mode to set newly-uploaded files to. The value should be a mode
 # you'd pass directly to os.chmod; see https://docs.python.org/library/os.html#files-and-directories.
-FILE_UPLOAD_PERMISSIONS = 0o644
+FILE_UPLOAD_PERMISSIONS = 0o755
 
 # The numeric mode to assign to newly-created directories, when uploading files.
 # The value should be a mode as you'd pass to os.chmod;
```
**变异语义**：将文件权限设置为 `0o755`（目录的典型权限值）。开发者可能混淆了文件权限和目录权限，将 `FILE_UPLOAD_DIRECTORY_PERMISSIONS` 的常见值 0o755 误用于文件权限，导致上传文件具有可执行位（rwxr-xr-x）。这在代码审查中不易发现，因为 0o755 是合法的 chmod 值，只是语义上用于目录而非文件。F2P 测试中 `assertEqual(file_permissions_mode, 0o644)` 会失败（0o755 ≠ 0o644）。

---

### Group E — 替换
**原 mutation**：
```diff
diff --git a/django/conf/global_settings.py b/django/conf/global_settings.py
index bdeec80610..c8493a4d7f 100644
--- a/django/conf/global_settings.py
+++ b/django/conf/global_settings.py
@@ -304,7 +304,7 @@ FILE_UPLOAD_TEMP_DIR = None
 
 # The numeric mode to set newly-uploaded files to. The value should be a mode
 # you'd pass directly to os.chmod; see https://docs.python.org/library/os.html#files-and-directories.
-FILE_UPLOAD_PERMISSIONS = 0o644
+FILE_UPLOAD_PERMISSIONS = 0o600
 
 # The numeri
```
**分类**：🟡 语义浅层（替换）
**理由**：与 Group A 的 diff（包括 index hash `c8493a4d7f`）完全相同，是精确重复。保留两个相同 mutation 无意义。

**最终 mutation**（替换为新 diff）：
```diff
diff --git a/django/conf/global_settings.py b/django/conf/global_settings.py
index bdeec80610..71dce96320 100644
--- a/django/conf/global_settings.py
+++ b/django/conf/global_settings.py
@@ -304,7 +304,7 @@ FILE_UPLOAD_TEMP_DIR = None
 
 # The numeric mode to set newly-uploaded files to. The value should be a mode
 # you'd pass directly to os.chmod; see https://docs.python.org/library/os.html#files-and-directories.
-FILE_UPLOAD_PERMISSIONS = 0o644
+FILE_UPLOAD_PERMISSIONS = 0o444
 
 # The numeric mode to assign to newly-created directories, when uploading files.
 # The value should be a mode as you'd pass to os.chmod;
```
**变异语义**：设置全只读权限（0o444）。开发者可能出于"上传文件不应被修改"的安全考量，将权限设置为只读，但这会导致应用程序后续无法删除或覆盖该文件（例如在 `FileSystemStorage.delete()` 中无法 `os.remove()`，或 Django 的文件覆盖逻辑失败）。F2P 测试中 `assertEqual(file_permissions_mode, 0o644)` 会断言失败（0o444 ≠ 0o644）。

## 新设计 Mutation 说明

### B_new (0o664)
**代码分析基础**：`file_permissions_mode` 最终值直接传给 `os.chmod`，任何非 0o644 的值都会导致 F2P 断言失败。选择 0o664 是因为它与 0o644 仅相差一个 bit（group write），视觉上极难区分，且真实场景下（web 服务器和应用运行在同一组时）开发者有时会认为组需要写权限。

### D_new (0o755)
**代码分析基础**：`global_settings.py` 中同时存在 `FILE_UPLOAD_PERMISSIONS` 和 `FILE_UPLOAD_DIRECTORY_PERMISSIONS`，0o755 是目录权限的标准值。开发者在复制粘贴或查阅文档时可能将两者混淆，将目录权限值误用于文件权限。这是真实的混淆型错误。

### E_new (0o444)
**代码分析基础**：安全意识强的开发者可能选择只读权限以防止上传文件被后续修改。0o444 在语义上与安全目标"一致"，但与实际需求（应用需要能删除/替换文件）相矛盾。这模拟了"过度安全化"的判断错误，在代码审查中看起来"有理由"。
