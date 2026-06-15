# astropy__astropy-13579

## 问题背景

`SlicedLowLevelWCS` 的 `world_to_pixel_values` 方法在处理维度耦合的 WCS 时结果错误。当用户对一个 3D WCS（空间×空间×波长，且 PCij 矩阵将光谱轴和空间轴耦合）做整数切片（如只取第 0 个波长平面），再对切片后的 2D WCS 调用 `world_to_pixel_values`，得到的像素坐标严重偏差（接近无穷大）。

根本原因：在 `world_to_pixel_values` 中，对于被切掉的世界维度（`iworld not in _world_keep`），原代码硬编码填入 `1.0` 作为占位值，而正确做法是填入该维度在切片像素位置对应的真实世界坐标。由于 PCij 矩阵的耦合，错误的占位值会影响所有耦合维度的像素坐标计算。

Golden patch 修复：在 `world_to_pixel_values` 开头调用 `_pixel_to_world_values_all(*[0]*len(self._pixel_keep))` 计算在切片参考点（全零像素）处所有世界维度的坐标，然后用这些真实坐标替换 `1.0` 占位值。

## Golden Patch 语义分析

**原错误**：`world_to_pixel_values` 在重建全维度世界坐标数组时，对"被切掉的世界维度"填入常数 `1.0`。对于维度完全解耦的 WCS 影响不大，但对于维度耦合的 WCS，`1.0` 是错误的参考值，导致底层 WCS 的 `world_to_pixel_values` 计算出错误结果。

**修复逻辑**：正确的参考值应当是：在切片所定义的固定像素坐标下，被切掉的世界维度的真实坐标值。这通过 `_pixel_to_world_values_all(*[0]*len(self._pixel_keep))` 实现——用全零像素坐标（切片后的参考点）调用 `_pixel_to_world_values_all`，得到所有世界维度的坐标，然后用 `sliced_out_world_coords[iworld]` 为被切掉的维度提供正确的世界坐标。

**为什么这样是正确的**：`_pixel_to_world_values_all` 内部会将切片后的像素坐标转换回原始 WCS 的像素坐标（加上 `slice.start` 偏移），因此得到的是原始 WCS 坐标系下正确的世界坐标。用这些值作为被切掉维度的参考，使得底层 `world_to_pixel_values` 能在正确的"切片平面"上工作。

## 调用链分析

```
SlicedLowLevelWCS.__init__
  └── 计算 _pixel_keep（非整数切片的像素轴）
  └── 计算 _world_keep（与保留像素轴相关的世界轴，基于 axis_correlation_matrix）

_pixel_to_world_values_all(*pixel_arrays)  ← 关键辅助函数
  └── 将切片后的像素坐标转换为原始坐标（加 slice.start 偏移，整数切片直接填值）
  └── 调用 self._wcs.pixel_to_world_values(...)  ← 底层 WCS，返回所有世界维度

pixel_to_world_values(*pixel_arrays)
  └── 调用 _pixel_to_world_values_all(...)
  └── 过滤 _world_keep 中的维度后返回

world_to_pixel_values(*world_arrays)  ← 修复点
  └── 调用 _pixel_to_world_values_all(*[0]*len(_pixel_keep))  ← golden patch 新增
  └── 重建全维度 world_arrays_new（用 sliced_out_world_coords 填补被切掉的维度）
  └── 调用 self._wcs.world_to_pixel_values(*world_arrays_new)
  └── 对 slice 类型的像素轴减去 slice.start 偏移
  └── 过滤 _pixel_keep 中的维度并返回
```

数据流：用户传入切片后的世界坐标 → 重建为原始 WCS 的全维度世界坐标 → 底层 WCS 计算全维度像素坐标 → 过滤保留维度并减去 slice.start 偏移 → 返回切片后的像素坐标。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | 将 `slice` 改为 `numbers.Integral` 类型检查，整数切片无 `.start` 属性，立即 AttributeError，不自然 |
| B | 必须替换 | 替换 | 去掉 `isinstance(..., slice)` 检查，整数切片无 `.start` 属性，立即 AttributeError，不自然 |
| C | 必须替换 | 替换 | 直接还原 golden patch 的关键修复（将 `sliced_out_world_coords[iworld]` 改回 `1.`），是 patch 的逆操作 |
| D | 高质量 | 保留 | 修改 `__init__` 中 `_world_keep` 的计算逻辑（不同函数），语义契约变异，影响整个对象生命周期 |

语义浅层共 0 个，无需按比例替换。3 个必须替换，均替换。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
diff --git a/astropy/wcs/wcsapi/wrappers/sliced_wcs.py b/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
index 773663f6e..555503b87 100644
--- a/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
+++ b/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
@@ -259,7 +259,7 @@ class SlicedLowLevelWCS(BaseWCSWrapper):
         pixel_arrays = list(self._wcs.world_to_pixel_values(*world_arrays_new))
 
         for ipixel in range(self._wcs.pixel_n_dim):
-            if isinstance(self._slices_pixel[ipixel], slice) and self._slices_pixel[ipixel].start is not None:
+            if isinstance(self._slices_pixel[ipixel], numbers.Integral) and self._slices_pixel[ipixel].start is not None:
                 pixel_arrays[ipixel] -= self._slices_pixel[ipixel].start
```

**分类**：🔴 必须替换

**理由**：`numbers.Integral` 类型的元素（整数切片）没有 `.start` 属性，代码会立即抛出 `AttributeError: 'int' object has no attribute 'start'`。这是明显的运行时崩溃，不符合"语义真实"的 mutation 设计原则。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/wcs/wcsapi/wrappers/sliced_wcs.py b/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
index 773663f6ec..9937f82d4c 100644
--- a/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
+++ b/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
@@ -218,10 +218,7 @@ class SlicedLowLevelWCS(BaseWCSWrapper):
                 pixel_arrays_new.append(self._slices_pixel[ipix])
             else:
                 ipix_curr += 1
-                if self._slices_pixel[ipix].start is not None:
-                    pixel_arrays_new.append(pixel_arrays[ipix_curr] + self._slices_pixel[ipix].start)
-                else:
-                    pixel_arrays_new.append(pixel_arrays[ipix_curr])
+                pixel_arrays_new.append(pixel_arrays[ipix_curr])
 
         pixel_arrays_new = np.broadcast_arrays(*pixel_arrays_new)
         return self._wcs.pixel_to_world_values(*pixel_arrays_new)
```

**变异语义**：`_pixel_to_world_values_all` 在将切片后的像素坐标转换为原始 WCS 坐标时，不再加上 `slice.start` 偏移。这是"忘记坐标系转换"的典型错误。对于 `slice.start = None` 或 `slice.start = 0` 的切片（包括整数切片 `wcs[0]`），行为完全正确；只有当切片有非零起始点时（如 `wcs[5:, :]`）才产生错误。此 mutation 影响 `pixel_to_world_values`、`world_to_pixel_values`（通过 `_pixel_to_world_values_all` 的调用）以及 `dropped_world_dimensions` 属性，是跨函数的状态传播变异。

---

### Group B — 替换

**原 mutation**：
```diff
diff --git a/astropy/wcs/wcsapi/wrappers/sliced_wcs.py b/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
index 773663f6e..5844f311c 100644
--- a/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
+++ b/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
@@ -259,7 +259,7 @@ class SlicedLowLevelWCS(BaseWCSWrapper):
         pixel_arrays = list(self._wcs.world_to_pixel_values(*world_arrays_new))
 
         for ipixel in range(self._wcs.pixel_n_dim):
-            if isinstance(self._slices_pixel[ipixel], slice) and self._slices_pixel[ipixel].start is not None:
+            if self._slices_pixel[ipixel].start is not None:
                 pixel_arrays[ipixel] -= self._slices_pixel[ipixel].start
```

**分类**：🔴 必须替换

**理由**：去掉了 `isinstance(..., slice)` 检查后，对整数切片（`numbers.Integral` 类型）调用 `.start` 属性，立即抛出 `AttributeError`。与 Group A 同样是不自然的运行时崩溃 mutation。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/wcs/wcsapi/wrappers/sliced_wcs.py b/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
index 773663f6ec..b8108f300f 100644
--- a/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
+++ b/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
@@ -253,7 +253,7 @@ class SlicedLowLevelWCS(BaseWCSWrapper):
                 iworld_curr += 1
                 world_arrays_new.append(world_arrays[iworld_curr])
             else:
-                world_arrays_new.append(sliced_out_world_coords[iworld])
+                world_arrays_new.append(sliced_out_world_coords[iworld_curr + 1])
 
         world_arrays_new = np.broadcast_arrays(*world_arrays_new)
         pixel_arrays = list(self._wcs.world_to_pixel_values(*world_arrays_new))
```

**变异语义**：在 `world_to_pixel_values` 中，对被切掉的世界维度填入参考坐标时，使用 `iworld_curr + 1`（已处理的保留维度计数器 + 1）而非 `iworld`（全局世界维度索引）。这是"混淆两个并行计数器语义"的典型错误。当被切掉的维度排在保留维度之后时（F2P 测试中的耦合 WCS：波长是第三世界维度，被切掉，而 `iworld_curr` 在 else 分支不递增），`iworld_curr + 1` 等于保留维度总数（2），而 `iworld` 等于 2，在此特例下恰好相等——但对于被切掉的维度排在保留维度之前的 WCS，两者不同，导致取到错误的参考世界坐标，使 `world_to_pixel_values` 返回错误结果。

---

### Group C — 替换

**原 mutation**：
```diff
diff --git a/astropy/wcs/wcsapi/wrappers/sliced_wcs.py b/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
index 773663f6e..6cb215ad5 100644
--- a/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
+++ b/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
@@ -244,8 +244,6 @@ class SlicedLowLevelWCS(BaseWCSWrapper):
 
     def world_to_pixel_values(self, *world_arrays):
         sliced_out_world_coords = self._pixel_to_world_values_all(*[0]*len(self._pixel_keep))
-
-        world_arrays = tuple(map(np.asanyarray, world_arrays))
         world_arrays_new = []
         iworld_curr = -1
         for iworld in range(self._wcs.world_n_dim):
@@ -253,7 +251,7 @@ def world_to_pixel_values(self, *world_arrays):
                 iworld_curr += 1
                 world_arrays_new.append(world_arrays[iworld_curr])
             else:
-                world_arrays_new.append(sliced_out_world_coords[iworld])
+                world_arrays_new.append(1.)
```

**分类**：🔴 必须替换

**理由**：这是 golden patch 的直接逆操作——将 `sliced_out_world_coords[iworld]` 改回 `1.`，同时去掉 `np.asanyarray` 转换。效果等同于还原到 base_commit 的原始 bug 代码，是典型的"直接冗余"mutation。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/wcs/wcsapi/wrappers/sliced_wcs.py b/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
index 773663f6ec..a586dc3be5 100644
--- a/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
+++ b/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
@@ -219,7 +219,7 @@ class SlicedLowLevelWCS(BaseWCSWrapper):
             else:
                 ipix_curr += 1
                 if self._slices_pixel[ipix].start is not None:
-                    pixel_arrays_new.append(pixel_arrays[ipix_curr] + self._slices_pixel[ipix].start)
+                    pixel_arrays_new.append(pixel_arrays[ipix_curr] - self._slices_pixel[ipix].start)
                 else:
                     pixel_arrays_new.append(pixel_arrays[ipix_curr])
```

**变异语义**：`_pixel_to_world_values_all` 在将切片后的像素坐标转换为原始 WCS 坐标时，将 `+ slice.start` 改为 `- slice.start`，偏移方向相反。这模拟了开发者在实现坐标系转换时弄反符号的典型错误。对于 `slice.start = None` 或 `slice.start = 0` 的切片（包括整数切片 `wcs[0]`），行为不变；只有当切片有非零起始点时才产生错误（错误的偏移量为 `2 * slice.start`）。此 mutation 影响 `pixel_to_world_values`、`world_to_pixel_values`（通过 `_pixel_to_world_values_all`）以及 `dropped_world_dimensions`，是跨函数的坐标系错误传播变异。

---

### Group D — 保留

**原 mutation**：
```diff
diff --git a/astropy/wcs/wcsapi/wrappers/sliced_wcs.py b/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
index 773663f6e..20619a300 100644
--- a/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
+++ b/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
@@ -146,8 +146,9 @@ class SlicedLowLevelWCS(BaseWCSWrapper):
                                        for ip in range(self._wcs.pixel_n_dim)])[0]
 
         # axis_correlation_matrix[world, pixel]
-        self._world_keep = np.nonzero(
-            self._wcs.axis_correlation_matrix[:, self._pixel_keep].any(axis=1))[0]
+        # self._world_keep = np.nonzero(
+        #     self._wcs.axis_correlation_matrix[:, self._pixel_keep].any(axis=1))[0]
+        self._world_keep = np.arange(self._wcs.world_n_dim)
```

**分类**：🟢 保留

**理由**：修改位置在 `__init__` 方法中，与 golden patch 修改的 `world_to_pixel_values` 是不同的函数和逻辑分支。改变了 `_world_keep` 的语义契约：原本只保留与保留像素维度相关的世界维度，现在保留所有世界维度。这会导致 `world_n_dim` 返回错误值，`world_to_pixel_values` 和 `pixel_to_world_values` 的输入/输出维度数不匹配，影响整个对象生命周期的所有方法。是多行修改、改变接口契约的高质量 mutation。

**最终 mutation**（与原相同）：
```diff
diff --git a/astropy/wcs/wcsapi/wrappers/sliced_wcs.py b/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
index 773663f6e..20619a300 100644
--- a/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
+++ b/astropy/wcs/wcsapi/wrappers/sliced_wcs.py
@@ -146,8 +146,9 @@ class SlicedLowLevelWCS(BaseWCSWrapper):
                                        for ip in range(self._wcs.pixel_n_dim)])[0]
 
         # axis_correlation_matrix[world, pixel]
-        self._world_keep = np.nonzero(
-            self._wcs.axis_correlation_matrix[:, self._pixel_keep].any(axis=1))[0]
+        # self._world_keep = np.nonzero(
+        #     self._wcs.axis_correlation_matrix[:, self._pixel_keep].any(axis=1))[0]
+        self._world_keep = np.arange(self._wcs.world_n_dim)
```

**变异语义**：`_world_keep` 从"仅保留与保留像素轴相关的世界轴"变为"保留所有世界轴"。被切掉的独立世界轴（如纯光谱轴）会被错误地包含在 `_world_keep` 中，导致 `world_n_dim` 虚增、`world_to_pixel_values` 期望更多的输入参数、`pixel_to_world_values` 返回更多的输出维度。对于耦合 WCS（F2P 测试场景），`test_coupled_world_slicing` 中 `sl.world_to_pixel_values(world[0], world[1])` 会因维度不匹配而失败（`world_n_dim` 变为 3 而非 2，期望 3 个输入但只提供了 2 个）。

## 新设计 Mutation 说明

### Group A 替换说明

**基于的代码分析**：`_pixel_to_world_values_all` 是 `pixel_to_world_values`、`world_to_pixel_values` 和 `dropped_world_dimensions` 的共同辅助函数。其核心职责是将切片坐标系的像素坐标转换为原始 WCS 坐标系的像素坐标，关键操作是 `pixel_arrays[ipix_curr] + self._slices_pixel[ipix].start`。

**选择此位置的原因**：去掉 `slice.start` 的偏移操作是"忘记坐标系转换"的典型错误。此错误只在切片有非零起始点时才显现，对于整数切片（如 `wcs[0]`）或无起始点的切片（如 `wcs[:]`）不影响结果，因此能通过 `test_coupled_world_slicing`（使用整数切片 `0`）。修改涉及多行（原来的 if/else 合并为一行），影响多个调用方，是跨函数的状态传播变异。

**模拟的真实开发者错误**：在实现坐标系变换时，认为切片后的像素坐标已经是绝对坐标，不需要再加偏移。

### Group B 替换说明

**基于的代码分析**：`world_to_pixel_values` 中，golden patch 的关键是用正确的世界坐标（`sliced_out_world_coords[iworld]`）替换被切掉维度的占位值。`sliced_out_world_coords` 是通过 `_pixel_to_world_values_all(*[0]*len(self._pixel_keep))` 计算的，返回的是按全局世界维度索引排列的数组，必须用 `iworld`（全局索引）来访问，而非 `iworld_curr`（保留维度计数器）。

**选择此位置的原因**：用 `iworld_curr + 1` 替换 `iworld` 是"混淆两个并行计数器语义"的典型错误。这个修改位于 golden patch 的核心修复点（`sliced_out_world_coords` 的索引），且对于 F2P 测试场景（被切掉的维度是最后一个世界维度）在数值上恰好相等（`iworld_curr + 1 == iworld == 2`），只有当被切掉维度不是最后一个时才暴露 bug，使得 mutation 难以被简单测试发现。

**模拟的真实开发者错误**：在维护多个并行计数器时，混淆了全局索引和局部计数器，误以为两者等价。

### Group C 替换说明

**基于的代码分析**：`_pixel_to_world_values_all` 中的 `+ slice.start` 是将切片坐标系的像素坐标（从 0 开始）转换为原始 WCS 坐标系（从 `slice.start` 开始）。这是一个坐标系平移操作，方向必须是加法。

**选择此位置的原因**：将 `+` 改为 `-` 是经典的符号错误，且在 `_pixel_to_world_values_all` 这个被多处调用的函数中，影响范围更广（`pixel_to_world_values`、`world_to_pixel_values`、`dropped_world_dimensions` 三处调用方均受影响）。对于 `slice.start = 0` 或 `None` 的切片，`+ 0 == - 0`，结果不变；只有非零起始切片才暴露 bug，使得简单测试难以检测。

**模拟的真实开发者错误**：在实现"切片坐标系 → 原始坐标系"的平移时，弄反了加减方向。
