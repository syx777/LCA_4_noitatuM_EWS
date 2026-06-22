# django__django-15503

## 问题背景

`JSONField` 的 `has_key`/`has_keys`/`has_any_keys` lookup 在 SQLite、MySQL、Oracle 上无法处理**数字字符串键**（如 `"123"`）。原因：`compile_json_path` 把能转成 int 的键当作数组下标 `[123]`，而 has_key 检查的对象其实是键名 `"123"`（应是 `."123"`）。Golden patch 引入 `compile_json_path_final_key`：把最后一个键以 `.<key>` 形式（字面键名）编译，并新增 `HasKeyOrArrayIndex` 子类供 isnull 等需要数组下标语义的场景使用。

## Golden Patch 语义分析

```python
class HasKeyLookup(PostgresOperatorLookup):
    def compile_json_path_final_key(self, key_transform):
        # Compile the final key without interpreting ints as array elements.
        return ".%s" % json.dumps(key_transform)

    def as_sql(...):
        ...
        *rhs_key_transforms, final_key = rhs_key_transforms
        rhs_json_path = compile_json_path(rhs_key_transforms, include_root=False)
        rhs_json_path += self.compile_json_path_final_key(final_key)
        rhs_params.append(lhs_json_path + rhs_json_path)
```
核心语义：**has_key 的最后一段键必须按"对象键名"编译（`.` + json 引号），而非按数组下标 `[n]`**。把 rhs 路径拆成"前缀 + final_key"，前缀走普通 `compile_json_path`，final_key 走新方法强制点号键名形式。`HasKeyOrArrayIndex` 覆写该方法回到数组下标语义，用于 isnull 兼容。

F2P 测试 `TestQuerying.test_has_key_number`：含数字键 `"123"`、嵌套 `"456"`、数组内 `"789"` 等，断言各 has_key/has_keys/has_any_keys 都能命中。

## 调用链分析

`HasKeyLookup.as_sql` 对每个 rhs key：拆出 final_key → 前缀 `compile_json_path` + `compile_json_path_final_key(final_key)`。`compile_json_path` 对每段做 int 判定（数字→`[n]`、非数字→`.key`）。final_key 通过新方法强制 `.key`，避免数字键被当下标。`KeyTransformIsNull` 的 oracle/sqlite 分支改用 `HasKeyOrArrayIndex` 保留下标语义。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `.%s`→`[%s]`，final_key 又被当数组下标，数字键 has_key 失败 |
| B | 🟢 高质量 | 保留 | 简化 `compile_json_path`，去掉 int/非 int 分支，所有键当 `.key` |
| C | 🟢 高质量 | 保留 | 移除 final_key 拆分，回到旧的整段 compile_json_path |
| D | 🟢 高质量 | 保留 | 在 final_key 中重新引入 int 判定，数字键转 `[n]` |
| E | ➕ 补充 | 新增 | 原缺 E 组（去掉 final_key 的 `.` 前导分隔符） |

原 A/B/C/D 四个机制各异且有效，补充 E。

## 各组 Mutation 分析

### Group A — 保留（A1 路径编译：当数组下标）
```diff
-        return ".%s" % json.dumps(key_transform)
+        return "[%s]" % json.dumps(key_transform)
```
**变异语义**：final_key 用 `[%s]`（数组下标语法）而非 `.%s`（对象键名）。数字键 `"123"` 被编译成 `[...]`，查询匹配数组第 123 元素而非键名 `"123"`，has_key 找不到。直接还原原 bug。保留。

### Group B — 保留（B2 移除 case：简化 compile_json_path）
```diff
-        try:
-            num = int(key_transform)
-        except ValueError:  # non-integer
-            path.append(".")
-            path.append(json.dumps(key_transform))
-        else:
-            path.append("[%s]" % num)
+        # Skip the try/except check for numeric keys
+        path.append(".")
+        path.append(json.dumps(key_transform))
```
**变异语义**：`compile_json_path` 去掉 int 分支，所有中间键都当 `.key`。看似与修复方向一致，但破坏了"真正的数组下标键"（如 `array__0`）——`0` 被当 `."0"` 而非 `[0]`，嵌套数组场景失败。保留。

### Group C — 保留（D-移除 final_key 拆分）
```diff
-            *rhs_key_transforms, final_key = rhs_key_transforms
             rhs_json_path = compile_json_path(rhs_key_transforms, include_root=False)
-            rhs_json_path += self.compile_json_path_final_key(final_key)
             rhs_params.append(lhs_json_path + rhs_json_path)
```
**变异语义**：移除 final_key 的拆分与特殊编译，整段走旧的 `compile_json_path`。final_key 若是数字字符串又被当数组下标，回到原 bug。撤销修复的核心拆分逻辑。保留。

### Group D — 保留（C1 类型：final_key 重引入 int 判定）
```diff
-        return ".%s" % json.dumps(key_transform)
+        try:
+            int(key_transform)
+            return "[%s]" % key_transform
+        except (ValueError, TypeError):
+            return ".%s" % json.dumps(key_transform)
```
**变异语义**：在 `compile_json_path_final_key` 内重新加 int 判定——数字键转 `[n]`。这正是 golden 要避免的：final_key 是数字时仍被当下标，has_key 失败。模拟"以为 final_key 也该支持数组下标"。保留。

### Group E — 补充（E1 测试期望：丢失 `.` 分隔符）
```diff
-        return ".%s" % json.dumps(key_transform)
+        return "%s" % json.dumps(key_transform)
```
**变异语义**：final_key 编译时去掉前导 `.` 分隔符。生成的 JSON path 形如 `$"123"` 而非 `$."123"`，缺少键访问分隔符，路径语法错误/不匹配，has_key 失败。模拟"拼字符串时漏了分隔符"。

## 新设计 Mutation 说明

原 A/B/C/D 覆盖"下标语法 / 简化路径 / 移除拆分 / 重引入 int 判定"四个机制且有效，全部保留。补充缺失的 E：去掉 final_key 的 `.` 分隔符使路径语法错误，与前四者正交（前者改键的"形态"，E 改路径"连接符"）。五组覆盖五个角度。全部实测：golden 通过、变异令 F2P（`test_has_key_number`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
