# django__django-11239

## 问题背景

Django 的 `dbshell` 管理命令在使用 PostgreSQL 后端时，调用 `psql` 命令行工具打开交互式 shell。Django 数据库配置支持通过 `OPTIONS` 或顶层字段传递 `sslmode`、`sslrootcert`、`sslcert`、`sslkey` 等 mTLS（双向 TLS）参数。然而 `DatabaseClient.runshell_db()` 仅将 `PGPASSWORD` 写入 subprocess 环境变量，缺少对 SSL 参数的处理，导致使用双向 TLS 认证的数据库连接在 `dbshell` 下无法工作。

Golden patch 的修复：在 `runshell_db` 中从 `conn_params` 提取 4 个 SSL 相关参数，并有条件地（非空时）写入 PostgreSQL 标准环境变量 `PGSSLMODE`、`PGSSLROOTCERT`、`PGSSLCERT`、`PGSSLKEY`。

## Golden Patch 语义分析

核心修复分两步：

1. **参数提取**（与 `password`/`user`/`host` 同级）：
   ```python
   sslmode = conn_params.get('sslmode', '')
   sslrootcert = conn_params.get('sslrootcert', '')
   sslcert = conn_params.get('sslcert', '')
   sslkey = conn_params.get('sslkey', '')
   ```
   直接从 `conn_params` 顶层取值，与 `get_connection_params()` 返回格式一致（Django 在 `DatabaseWrapper.get_connection_params()` 中把 OPTIONS 里的参数平铺到顶层）。

2. **条件设置环境变量**：只有参数非空时才写入 env，避免将空字符串覆盖系统级 SSL 配置。这与 `PGPASSWORD` 的处理方式一致。

为什么"有条件设置"是正确的：psql 读取环境变量时，空字符串也会被解释为有效值，可能覆盖 `~/.pgpass` 或系统 SSL 配置；而未设置的变量则回退到默认行为。

## 调用链分析

```
DatabaseClient.runshell()
  └─ DatabaseClient.runshell_db(conn_params)   ← golden patch 修改点
       ├─ conn_params 来源: DatabaseWrapper.get_connection_params()
       │    └─ 将 OPTIONS 中的 ssl* 参数平铺至顶层 dict
       └─ subprocess.run(args, env=subprocess_env)
            └─ psql 读取 PGSSLMODE/PGSSLROOTCERT/PGSSLCERT/PGSSLKEY
```

关键数据流：`settings.DATABASES['default']['OPTIONS']['sslcert']` → `get_connection_params()` 返回的 dict 的顶层 `sslcert` 键 → `runshell_db` 中的 `subprocess_env['PGSSLCERT']`。

## 替换决策总览

本实例 mutations.jsonl 中仅存在 2 条记录（Group A 和 D），且两者均为低质量：

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原 A 将 `PGSSLCERT` 改为 `PGSSLCERTIFICATE`，是单一错误变量名，过于表面，psql 会直接忽略未知变量名 |
| B | 新设计 | 新增 | 无原始 mutation，新增一个高质量 mutation |
| C | 新设计 | 新增 | 无原始 mutation，新增一个高质量 mutation |
| D | 🔴 必须替换 | 替换 | 原 D 将 sslcert 提取行注释掉，代码中保留注释行，属于"不自然"的人工痕迹 |
| E | 新设计 | 新增 | 无原始 mutation，新增一个高质量 mutation |

（本实例实质上是路径 B，5 个 mutation 全部重新设计）

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
diff --git a/django/db/backends/postgresql/client.py b/django/db/backends/postgresql/client.py
index 0efe0d47f0..aabbfd6321 100644
--- a/django/db/backends/postgresql/client.py
+++ b/django/db/backends/postgresql/client.py
@@ -39,7 +39,7 @@ class DatabaseClient(BaseDatabaseClient):
         if sslrootcert:
             subprocess_env['PGSSLROOTCERT'] = str(sslrootcert)
         if sslcert:
-            subprocess_env['PGSSLCERT'] = str(sslcert)
+            subprocess_env['PGSSLCERTIFICATE'] = str(sslcert)
         if sslkey:
             subprocess_env['PGSSLKEY'] = str(sslkey)
```
**分类**：🔴 必须替换  
**理由**：`PGSSLCERTIFICATE` 是无效的 PostgreSQL 环境变量（正确名称是 `PGSSLCERT`），psql 会直接忽略该变量，相当于完全没有设置客户端证书。这个错误非常表面：任何熟悉 libpq 环境变量的开发者都会立即发现，且测试只需检查 env dict 的 key 是否正确即可捕获。不符合"难以发现"的标准。

**最终 mutation**（新设计）：
```diff
diff --git a/django/db/backends/postgresql/client.py b/django/db/backends/postgresql/client.py
index 0efe0d47f0..b585481608 100644
--- a/django/db/backends/postgresql/client.py
+++ b/django/db/backends/postgresql/client.py
@@ -34,10 +34,8 @@ class DatabaseClient(BaseDatabaseClient):
         subprocess_env = os.environ.copy()
         if passwd:
             subprocess_env['PGPASSWORD'] = str(passwd)
-        if sslmode:
-            subprocess_env['PGSSLMODE'] = str(sslmode)
-        if sslrootcert:
-            subprocess_env['PGSSLROOTCERT'] = str(sslrootcert)
+        subprocess_env['PGSSLMODE'] = str(sslmode)
+        subprocess_env['PGSSLROOTCERT'] = str(sslrootcert)
         if sslcert:
             subprocess_env['PGSSLCERT'] = str(sslcert)
         if sslkey:
```
**变异语义**：`sslmode` 和 `sslrootcert` 被无条件设置到环境变量（即使值为空字符串）。当用户未配置这两个参数时，会把空字符串 `''` 写入 `PGSSLMODE` 和 `PGSSLROOTCERT`，psql 会将其解释为有效值（`sslmode=''` 会报错或使用默认行为），可能导致无 SSL 配置的连接失败，或覆盖系统级 SSL 配置。对于明确配置了 `sslmode` 的用户，测试会通过；只有在不传 `sslmode` 的测试场景（如 `test_basic`、`test_nopass`）下才会暴露问题。代码审查时看起来像是"少了两个 if 判断的小疏漏"，与 `PGPASSWORD` 的条件设置形成不一致但不明显。

---

### Group B — 新增

**最终 mutation**：
```diff
diff --git a/django/db/backends/postgresql/client.py b/django/db/backends/postgresql/client.py
index 0efe0d47f0..5d60d74bb4 100644
--- a/django/db/backends/postgresql/client.py
+++ b/django/db/backends/postgresql/client.py
@@ -17,10 +17,11 @@ class DatabaseClient(BaseDatabaseClient):
         dbname = conn_params.get('database', '')
         user = conn_params.get('user', '')
         passwd = conn_params.get('password', '')
-        sslmode = conn_params.get('sslmode', '')
-        sslrootcert = conn_params.get('sslrootcert', '')
-        sslcert = conn_params.get('sslcert', '')
-        sslkey = conn_params.get('sslkey', '')
+        options = conn_params.get('OPTIONS', {})
+        sslmode = options.get('sslmode', '')
+        sslrootcert = options.get('sslrootcert', '')
+        sslcert = options.get('sslcert', '')
+        sslkey = options.get('sslkey', '')
 
         if user:
             args += ['-U', user]
```
**变异语义**：SSL 参数从 `conn_params.get('OPTIONS', {})` 的子字典中读取，而非从 `conn_params` 顶层读取。这是一个非常真实的开发者错误——用户在 Django settings 中确实是把 ssl 参数写在 `OPTIONS` 里，而 `get_connection_params()` 会将其平铺到顶层。一个不熟悉 Django PostgreSQL 后端内部行为的开发者（参考其他 `database`/`user`/`password` 从 `conn_params` 取的方式，却不知道 OPTIONS 已被展开）极可能写出此代码。`test_basic`/`test_nopass`/`test_column` 等不含 SSL 的测试会全部通过；`test_ssl_certificate` 会失败因为 `conn_params` 中没有 `OPTIONS` 键，所有 ssl 变量为空。

---

### Group C — 新增

**最终 mutation**：
```diff
diff --git a/django/db/backends/postgresql/client.py b/django/db/backends/postgresql/client.py
index 0efe0d47f0..1d4473662d 100644
--- a/django/db/backends/postgresql/client.py
+++ b/django/db/backends/postgresql/client.py
@@ -39,9 +39,9 @@ class DatabaseClient(BaseDatabaseClient):
         if sslrootcert:
             subprocess_env['PGSSLROOTCERT'] = str(sslrootcert)
         if sslcert:
-            subprocess_env['PGSSLCERT'] = str(sslcert)
+            subprocess_env['PGSSLKEY'] = str(sslcert)
         if sslkey:
-            subprocess_env['PGSSLKEY'] = str(sslkey)
+            subprocess_env['PGSSLCERT'] = str(sslkey)
```
**变异语义**：`PGSSLCERT`（客户端证书路径）和 `PGSSLKEY`（客户端私钥路径）的赋值对象被对调——证书文件路径写入了 `PGSSLKEY`，私钥路径写入了 `PGSSLCERT`。这模拟了一个拷贝粘贴错误或行序颠倒的错误，代码逻辑结构完全对称，阅读时很难发现。psql 会用错误的文件作为证书/密钥，导致 TLS 握手失败（文件类型不匹配），但其他非 mTLS 测试（不检查这两个 env 的顺序/内容时）会通过。

---

### Group D — 替换

**原 mutation**：
```diff
diff --git a/django/db/backends/postgresql/client.py b/django/db/backends/postgresql/client.py
index 0efe0d47f0..1d39fecd8c 100644
--- a/django/db/backends/postgresql/client.py
+++ b/django/db/backends/postgresql/client.py
@@ -19,7 +19,7 @@ class DatabaseClient(BaseDatabaseClient):
         passwd = conn_params.get('password', '')
         sslmode = conn_params.get('sslmode', '')
         sslrootcert = conn_params.get('sslrootcert', '')
-        sslcert = conn_params.get('sslcert', '')
+        # sslcert = conn_params.get("sslcert", "")
         sslkey = conn_params.get('sslkey', '')
```
**分类**：🔴 必须替换  
**理由**：将代码行直接注释掉，留下注释行，既不自然（真实代码中不会有这种遗留注释），又在语法上使 `sslcert` 变量未定义（后续 `if sslcert:` 会抛出 `NameError`），测试会立即以异常失败而非行为差异失败，属于"不合法"的 mutation。

**最终 mutation**（新设计）：
```diff
diff --git a/django/db/backends/postgresql/client.py b/django/db/backends/postgresql/client.py
index 0efe0d47f0..995a31f120 100644
--- a/django/db/backends/postgresql/client.py
+++ b/django/db/backends/postgresql/client.py
@@ -18,7 +18,7 @@ class DatabaseClient(BaseDatabaseClient):
         user = conn_params.get('user', '')
         passwd = conn_params.get('password', '')
         sslmode = conn_params.get('sslmode', '')
-        sslrootcert = conn_params.get('sslrootcert', '')
+        sslrootcert = conn_params.get('ssl_root_cert', '')
         sslcert = conn_params.get('sslcert', '')
         sslkey = conn_params.get('sslkey', '')
```
**变异语义**：`sslrootcert` 从 `conn_params.get('ssl_root_cert', '')` 中读取（下划线分隔），而 Django `get_connection_params()` 实际传入的键是 `'sslrootcert'`（无下划线，与 libpq 关键字一致）。结果是 `sslrootcert` 总为空字符串，CA 根证书路径无法设置，导致 `PGSSLROOTCERT` 不会被写入环境，TLS 证书链验证会失败（当 `sslmode=verify-ca` 或 `verify-full` 时）。代码看起来合理——`ssl_root_cert` 这种命名在其他数据库驱动中很常见，阅读者容易忽略有无下划线的区别。

---

### Group E — 新增

**最终 mutation**：
```diff
diff --git a/django/db/backends/postgresql/client.py b/django/db/backends/postgresql/client.py
index 0efe0d47f0..9b615d4f3c 100644
--- a/django/db/backends/postgresql/client.py
+++ b/django/db/backends/postgresql/client.py
@@ -34,9 +34,9 @@ class DatabaseClient(BaseDatabaseClient):
         subprocess_env = os.environ.copy()
         if passwd:
             subprocess_env['PGPASSWORD'] = str(passwd)
-        if sslmode:
-            subprocess_env['PGSSLMODE'] = str(sslmode)
         if sslrootcert:
+            subprocess_env['PGSSLMODE'] = str(sslmode)
+        if sslmode:
             subprocess_env['PGSSLROOTCERT'] = str(sslrootcert)
         if sslcert:
             subprocess_env['PGSSLCERT'] = str(sslcert)
```
**变异语义**：`PGSSLMODE` 的条件守卫变为 `sslrootcert`（仅当 root cert 非空时设置 sslmode），`PGSSLROOTCERT` 的条件守卫变为 `sslmode`（仅当 sslmode 非空时设置 rootcert）。这是一个守卫条件互换的错误——当 `sslrootcert` 为空但 `sslmode` 不为空时（例如 `sslmode='require'` 但不需要 CA 验证），`PGSSLMODE` 不会被设置，连接模式会被忽略。反之，当 `sslmode` 非空但 `sslrootcert` 为空时，`PGSSLROOTCERT` 不会被设置（这实际是正确的），但 `PGSSLMODE` 会被设置（守卫为 `sslrootcert` 为空时不设置）。对于完整配置（sslmode+sslrootcert 均非空）的测试会通过；只有在单独设置 `sslmode` 而不设置 `sslrootcert` 的场景下才会失败。代码结构看起来仅是两个 if 块的顺序变化，不易察觉守卫逻辑的偏移。

## 新设计 Mutation 说明

**Group A**：基于观察 golden patch 中 `PGPASSWORD` 使用了 `if passwd:` 守卫而 SSL 参数同样需要守卫，设计了"遗漏 sslmode/sslrootcert 守卫"的 mutation。选择仅去掉前两个守卫（sslmode/sslrootcert）而保留 sslcert/sslkey 的守卫，使行为不一致性更隐蔽。

**Group B**：基于 Django settings.py 中 ssl 参数确实在 `OPTIONS` 字典中的事实，模拟了"对 `get_connection_params()` 不熟悉的开发者"可能犯的错误——误以为需要从 `OPTIONS` 子字典读取而非顶层平铺的 `conn_params`。这是涉及 Django 数据库连接参数传递机制的语义误解，属于接口契约变异。

**Group C**：模拟了拷贝粘贴时将 cert/key 赋值行对调的错误，两行结构完全对称，代码审查中需要特别注意变量名与环境变量名的对应关系才能发现。

**Group D**：基于 libpq 参数命名与其他数据库驱动命名差异（PostgreSQL 使用 `sslrootcert` 而非 `ssl_root_cert`），模拟了命名规范混淆的错误，选择 `sslrootcert` 而非 `sslcert`/`sslkey` 是因为其他参数名无法产生合理的下划线变体。

**Group E**：模拟了在整理 if 条件块时发生的"守卫变量名"混淆错误，开发者可能在代码格式化或重排过程中意外将两个 if 条件对调，而内部赋值语句保持不变。
