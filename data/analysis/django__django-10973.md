# django__django-10973

## 问题背景

该 issue 要求将 `django.db.backends.postgresql.client` 中的密码传递机制从"临时 `.pgpass` 文件"方式重构为直接使用环境变量 `PGPASSWORD`，并将 `subprocess.check_call` 替换为支持自定义环境的 `subprocess.run`。

**修复前**：通过创建临时文件写入 pgpass 格式的密码，设置 `os.environ['PGPASSFILE']`，调用 `subprocess.check_call(args)`，最后删除临时文件和环境变量。

**修复后**：`os.environ.copy()` 创建隔离的环境副本，将 `PGPASSWORD` 直接写入副本，通过 `subprocess.run(args, check=True, env=subprocess_env)` 传递给子进程。父进程环境不被污染。

## Golden Patch 语义分析

核心修复逻辑：

1. **环境隔离**：使用 `os.environ.copy()` 而非直接修改 `os.environ`，确保父进程环境不被 `PGPASSWORD` 污染。
2. **密码传递机制替换**：废弃了依赖文件系统的 `.pgpass` 临时文件方式，改用内核级环境变量传递，更可靠（无文件权限问题、无 UnicodeEncodeError 静默失败风险）。
3. **subprocess API 升级**：`subprocess.check_call` → `subprocess.run(..., check=True, env=...)` 允许传入自定义环境字典。
4. **代码简化**：删除 `NamedTemporaryFile`、`_escape_pgpass`、`PGPASSFILE` 清理逻辑等约40行代码。

修复的关键语义：`PGPASSWORD` 必须写入一个 **os.environ 的副本**，并将该副本作为 `env` 参数传递给 `subprocess.run`。

## 调用链分析

```
DatabaseClient.runshell()
    └── DatabaseClient.runshell_db(conn_params)
            ├── conn_params.get('password', '') → passwd
            ├── os.environ.copy() → subprocess_env
            ├── subprocess_env['PGPASSWORD'] = str(passwd)  [if passwd]
            ├── signal.signal(SIGINT, SIG_IGN)   [before subprocess]
            ├── subprocess.run(args, check=True, env=subprocess_env)
            └── signal.signal(SIGINT, sigint_handler)  [in finally]
```

被修改的函数只有 `runshell_db`，上游调用者是 `runshell`（通过 `self.connection.get_connection_params()` 获取 conn_params）。无外部依赖，逻辑完全自包含。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | `os.environ` vs `.copy()` 对测试完全不可见：mock 捕获 env 时已能看到 PGPASSWORD，断言通过，mutation 无效 |
| B | 语义浅层 | 保留 | `if passwd:` → `if passwd is not None:` 在边界值（空字符串密码）下制造真实错误，位置关键 |
| C | 缺失 | 新建 | 原 mutations.jsonl 中无 Group C，需创建 |
| D | 必须替换 | 替换 | 删除 `subprocess_env = os.environ.copy()` 行导致 NameError，明显崩溃，不是真实开发者错误 |
| E | 语义浅层 | 替换 | 添加 `use_password=False` 参数的 feature-flag 模式过于人工化，在代码审查中会立即被发现 |

语义浅层共 2 个（B、E），替换其中最弱的 floor(2/2) = 1 个：**E 被替换**（feature-flag 模式明显不自然）。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
diff --git a/django/db/backends/postgresql/client.py b/django/db/backends/postgresql/client.py
index cf4df76882..d37dccbcce 100644
--- a/django/db/backends/postgresql/client.py
+++ b/django/db/backends/postgresql/client.py
@@ -27,7 +27,7 @@ class DatabaseClient(BaseDatabaseClient):
         args += [dbname]
 
         sigint_handler = signal.getsignal(signal.SIGINT)
-        subprocess_env = os.environ.copy()
+        subprocess_env = os.environ
         if passwd:
             subprocess_env['PGPASSWORD'] = str(passwd)
         try:
```

**分类**：🔴 必须替换

**理由**：`subprocess_env = os.environ`（不 copy）意味着 `subprocess_env['PGPASSWORD'] = str(passwd)` 实际修改了 `os.environ`。测试 mock `_mock_subprocess_run(*args, env=os.environ, **kwargs)` 中参数 `env` 接收到的是 `os.environ`（本就是同一对象），`env.get('PGPASSWORD')` 返回的是已被修改后的值，断言通过。**所有 F2P 测试均会通过**，这个 mutation 对测试不可见，无法作为有效变异。

**最终 mutation**（新设计）：
```diff
diff --git a/django/db/backends/postgresql/client.py b/django/db/backends/postgresql/client.py
index cf4df76882..78138e6caf 100644
--- a/django/db/backends/postgresql/client.py
+++ b/django/db/backends/postgresql/client.py
@@ -29,7 +29,7 @@ class DatabaseClient(BaseDatabaseClient):
         sigint_handler = signal.getsignal(signal.SIGINT)
         subprocess_env = os.environ.copy()
         if passwd:
-            subprocess_env['PGPASSWORD'] = str(passwd)
+            subprocess_env['PGPASS'] = str(passwd)
         try:
             # Allow SIGINT to pass to psql to abort queries.
             signal.signal(signal.SIGINT, signal.SIG_IGN)
```

**变异语义**：将环境变量键名从 `PGPASSWORD`（PostgreSQL 标准密码变量）改为 `PGPASS`（无效键名）。密码被设置在错误的 key 下，psql 实际上看不到密码。测试检查 `env.get('PGPASSWORD')` → 返回 `None`（而非密码字符串），`test_basic`、`test_column`、`test_accent` 均失败。代码看起来完全合理，开发者可能误以为是 pgpass 缩写。

---

### Group B — 保留

**原 mutation**：
```diff
diff --git a/django/db/backends/postgresql/client.py b/django/db/backends/postgresql/client.py
index cf4df76882..333feba0eb 100644
--- a/django/db/backends/postgresql/client.py
+++ b/django/db/backends/postgresql/client.py
@@ -28,7 +28,7 @@ class DatabaseClient(BaseDatabaseClient):
 
         sigint_handler = signal.getsignal(signal.SIGINT)
         subprocess_env = os.environ.copy()
-        if passwd:
+        if passwd is not None:
             subprocess_env['PGPASSWORD'] = str(passwd)
         try:
             # Allow SIGINT to pass to psql to abort queries.
```

**分类**：🟡 语义浅层（保留）

**理由**：`if passwd:` 处于密码设置的关键判断节点。`if passwd is not None:` 与 `if passwd:` 的差异在于空字符串 `''` 的处理：前者会将空字符串也写入 `PGPASSWORD`，后者不会。这模拟了开发者对 Python 真值检查与 `None` 检查语义的混淆，是常见的真实错误。测试中 `test_nopass` 使用无 password 键的 dict，`passwd = ''`（默认值），`if passwd is not None:` 为 True，`PGPASSWORD=''` 被设置，测试期望 PGPASSWORD 为 None → 失败。有效变异，保留。

**最终 mutation**（与原相同）：
```diff
diff --git a/django/db/backends/postgresql/client.py b/django/db/backends/postgresql/client.py
index cf4df76882..333feba0eb 100644
--- a/django/db/backends/postgresql/client.py
+++ b/django/db/backends/postgresql/client.py
@@ -28,7 +28,7 @@ class DatabaseClient(BaseDatabaseClient):
 
         sigint_handler = signal.getsignal(signal.SIGINT)
         subprocess_env = os.environ.copy()
-        if passwd:
+        if passwd is not None:
             subprocess_env['PGPASSWORD'] = str(passwd)
         try:
             # Allow SIGINT to pass to psql to abort queries.
```

**变异语义**：`test_nopass` 中 `conn_params` 无 'password' 键，`passwd = conn_params.get('password', '')` 返回 `''`。`if passwd is not None:` → True，`subprocess_env['PGPASSWORD'] = ''`，测试断言 PGPASSWORD 为 None → 失败。其余有密码的测试仍通过。

---

### Group C — 新建

**原 mutation**：（无，此组在 mutations.jsonl 中缺失）

**分类**：新建

**最终 mutation**：
```diff
diff --git a/django/db/backends/postgresql/client.py b/django/db/backends/postgresql/client.py
index cf4df76882..4f1a7aca05 100644
--- a/django/db/backends/postgresql/client.py
+++ b/django/db/backends/postgresql/client.py
@@ -36,7 +36,7 @@ class DatabaseClient(BaseDatabaseClient):
             subprocess.run(args, check=True, env=subprocess_env)
         finally:
             # Restore the original SIGINT handler.
-            signal.signal(signal.SIGINT, sigint_handler)
+            signal.signal(signal.SIGINT, signal.SIG_DFL)
 
     def runshell(self):
         DatabaseClient.runshell_db(self.connection.get_connection_params())
```

**变异语义**：在 `finally` 中将 SIGINT 恢复为系统默认处理器 `SIG_DFL`，而非保存的原始处理器。`test_sigint_handler` 在调用完成后检查 `signal.getsignal(signal.SIGINT) == sigint_handler`（原始 handler）。恢复为 SIG_DFL 后，两者不等，测试失败。这模拟了开发者"将信号重置到系统默认"而非"恢复原始处理器"的误解，是信号处理中的经典错误。

---

### Group D — 替换

**原 mutation**：
```diff
diff --git a/django/db/backends/postgresql/client.py b/django/db/backends/postgresql/client.py
index cf4df76882..5b20dfe8cb 100644
--- a/django/db/backends/postgresql/client.py
+++ b/django/db/backends/postgresql/client.py
@@ -27,7 +27,6 @@ class DatabaseClient(BaseDatabaseClient):
         args += [dbname]
 
         sigint_handler = signal.getsignal(signal.SIGINT)
-        subprocess_env = os.environ.copy()
         if passwd:
             subprocess_env['PGPASSWORD'] = str(passwd)
         try:
```

**分类**：🔴 必须替换

**理由**：删除 `subprocess_env = os.environ.copy()` 导致后续 `subprocess_env['PGPASSWORD']` 抛出 `NameError: name 'subprocess_env' is not defined`，以及 `subprocess.run(args, check=True, env=subprocess_env)` 也抛出 NameError。这是立即崩溃的语法级错误，不是真实开发者会引入的 bug。即使在 no-password 路径中（跳过 if passwd 块），`env=subprocess_env` 也会崩溃。完全不自然。

**最终 mutation**（新设计）：
```diff
diff --git a/django/db/backends/postgresql/client.py b/django/db/backends/postgresql/client.py
index cf4df76882..195ed1b201 100644
--- a/django/db/backends/postgresql/client.py
+++ b/django/db/backends/postgresql/client.py
@@ -31,9 +31,9 @@ class DatabaseClient(BaseDatabaseClient):
         if passwd:
             subprocess_env['PGPASSWORD'] = str(passwd)
         try:
+            subprocess.run(args, check=True, env=subprocess_env)
             # Allow SIGINT to pass to psql to abort queries.
             signal.signal(signal.SIGINT, signal.SIG_IGN)
-            subprocess.run(args, check=True, env=subprocess_env)
         finally:
             # Restore the original SIGINT handler.
             signal.signal(signal.SIGINT, sigint_handler)
```

**变异语义**：将 `subprocess.run` 移到 `signal.signal(SIG_IGN)` 之前，使 SIGINT 在 psql 执行期间仍为原始处理器而非 SIG_IGN。`test_sigint_handler` 在 mock 执行时检查 `signal.getsignal(signal.SIGINT) == signal.SIG_IGN`，但此时还未设置，检查失败。这是真实的初始化顺序错误，代码逻辑看起来完全正常，注释也只是位置稍移，极难在 review 中发现。

---

### Group E — 替换

**原 mutation**：
```diff
diff --git a/django/db/backends/postgresql/client.py b/django/db/backends/postgresql/client.py
index cf4df76882..19a51f8984 100644
--- a/django/db/backends/postgresql/client.py
+++ b/django/db/backends/postgresql/client.py
@@ -9,7 +9,7 @@ class DatabaseClient(BaseDatabaseClient):
     executable_name = 'psql'
 
     @classmethod
-    def runshell_db(cls, conn_params):
+    def runshell_db(cls, conn_params, use_password=False):
         args = [cls.executable_name]
 
         host = conn_params.get('host', '')
@@ -28,7 +28,7 @@ class DatabaseClient(BaseDatabaseClient):
 
         sigint_handler = signal.getsignal(signal.SIGINT)
         subprocess_env = os.environ.copy()
-        if passwd:
+        if passwd and use_password:
             subprocess_env['PGPASSWORD'] = str(passwd)
         try:
             # Allow SIGINT to pass to psql to abort queries.
```

**分类**：🟡 语义浅层（替换，最弱）

**理由**：添加 `use_password=False` 参数作为 feature flag，默认禁用密码传递。虽然修改了两处（函数签名+条件），但模式过于刻意——任何代码审查者看到 `use_password=False` 默认值就会立即质疑。这是一个人工味浓重的 feature flag mutation，而非真实开发者错误。

**最终 mutation**（新设计）：
```diff
diff --git a/django/db/backends/postgresql/client.py b/django/db/backends/postgresql/client.py
index cf4df76882..c8d4e78248 100644
--- a/django/db/backends/postgresql/client.py
+++ b/django/db/backends/postgresql/client.py
@@ -26,13 +26,13 @@ class DatabaseClient(BaseDatabaseClient):
             args += ['-p', str(port)]
         args += [dbname]
 
-        sigint_handler = signal.getsignal(signal.SIGINT)
         subprocess_env = os.environ.copy()
         if passwd:
             subprocess_env['PGPASSWORD'] = str(passwd)
         try:
             # Allow SIGINT to pass to psql to abort queries.
             signal.signal(signal.SIGINT, signal.SIG_IGN)
+            sigint_handler = signal.getsignal(signal.SIGINT)
             subprocess.run(args, check=True, env=subprocess_env)
         finally:
             # Restore the original SIGINT handler.
```

**变异语义**：将 `sigint_handler = signal.getsignal(signal.SIGINT)` 的赋值移到 `signal.signal(SIG_IGN)` 之后。此时捕获的 `sigint_handler` 是 `SIG_IGN` 而非原始处理器。在 `finally` 中，`signal.signal(SIGINT, sigint_handler)` 将信号恢复为 `SIG_IGN`（而非原始值）。`test_sigint_handler` 调用后检查 `signal.getsignal(SIGINT) == original_handler`，但此时是 `SIG_IGN`，断言失败。这是极为隐蔽的错误：save-before-modify 与 save-after-modify 的顺序颠倒，代码视觉上几乎完全相同。

---

## 新设计 Mutation 说明

### Group A 新设计

基于对 golden patch 核心语义的分析：环境变量 **键名** `PGPASSWORD` 是 PostgreSQL 识别密码的唯一标准（man 7 envvar），将其改为 `PGPASS` 是一个看似合理实则完全无效的 key。

- **选择位置**：`subprocess_env['PGPASSWORD'] = str(passwd)` 这行在代码视觉上正确，只是 key 名称微变
- **模拟的真实错误**：开发者可能混淆了 PostgreSQL 的不同认证文件（`.pgpass` 文件 vs `PGPASSWORD` 环境变量），将变量名写成 `PGPASS`（类 pgpass 缩写）
- **检测难度**：需要熟悉 PostgreSQL 的环境变量规范才能发现，代码本身语法/逻辑完全正确

### Group C 新建

基于 SIGINT 信号处理逻辑分析：`finally` 块中 "恢复原始处理器" vs "重置到系统默认" 是典型的混淆场景。

- **选择位置**：`finally` 中 `signal.signal(signal.SIGINT, sigint_handler)` 的参数
- **模拟的真实错误**：开发者可能认为 "finally 要清理资源，把信号重置回系统默认" 是合理操作，忽略了应该恢复到调用前的状态
- **检测难度**：只有当原始处理器不是 SIG_DFL 时（Python 进程通常如此）才会失败，视觉上 `signal.SIG_DFL` 也很自然

### Group D 新设计

基于 try 块内初始化顺序分析：`signal.signal` 和 `subprocess.run` 的相对顺序在视觉上不带任何依赖关系标记。

- **选择位置**：try 块内两行代码的顺序
- **模拟的真实错误**：开发者在重构代码时可能认为"先运行 psql 再设置信号处理无所谓"，或在代码合并时行顺序颠倒
- **检测难度**：两行都在 try 块中，注释也随之移动，格式完全一致，只有了解 psql SIGINT 行为才能发现问题

### Group E 新设计

基于 save-use-restore 模式分析：正确的信号处理是 save→modify→use→restore，本变异将 save 移到 modify 之后。

- **选择位置**：`sigint_handler = signal.getsignal(signal.SIGINT)` 的时序位置
- **模拟的真实错误**：开发者可能认为 "sigint_handler 只需要在 finally 使用，所以在 try 里声明更合理（局部性）"，这是对 Python 作用域规则的误用结合信号处理语义的混淆
- **检测难度**：与 Group D 效果相近（都使 test_sigint_handler 失败），但机制完全不同，互为补充；代码视觉差异极小（一行上移了5行）
