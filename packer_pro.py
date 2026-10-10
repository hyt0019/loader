# -*- coding: utf-8 -*-
"""
集装箱装箱计算程序（生产版 packer_pro）
================================================================
本文件在整数毫米几何内核之上，提供两种装箱模式，并可在启动时选择：

  1) 标准版（deterministic）
     复现最初版(Decimal 版)的逐箱装箱决策，结果稳定、可复现、速度快。

  2) 增强版（混合启发式搜索）
     组合混合列模式、二维排样、有界束搜索、列/层续装与遗传重启逼近最优。
     以标准版的解作为种子并采用【精英保留】，只有严格更优才替换，
     并在全部货物装入时立即停止——因此增强版在数学上保证【不劣于】标准版。

几何内核：所有几何量使用整数毫米(mm)，碰撞检测用均匀网格空间索引，
支撑检测用"顶面高度表"，候选位置用增量维护的极点集合。
支撑规则：非落地货物的底面中心必须落在下方接触面的凸包内（重心有支撑，不会悬空）。

块构建束搜索（block_engine.py）：同类货物拼成整齐的块，用极大空闲空间记录剩余
空间，束搜索逐轮加宽；标准版取其贪心结果与原有逐箱策略中更好者，增强版持续搜索。
================================================================
"""

import numpy as np
from itertools import permutations
from decimal import Decimal
from collections import Counter, defaultdict
import json
import math
import os
import hashlib
import random
import time

from block_engine import BlockSearch, center_supported

_plt = None


def _pyplot():
    """桌面可视化才加载 Matplotlib；网页后端与计算子进程不需要它，启动更快、更省内存。"""
    global _plt
    if _plt is None:
        import matplotlib
        try:
            matplotlib.use('TkAgg')  # 桌面交互式后端
        except Exception:
            pass  # 无图形环境时退回默认后端
        import matplotlib.pyplot as plt
        _plt = plt
    return _plt

MM = 1000  # 米 -> 毫米


# --------------------------------------------------------------------------- #
#                               通用小工具                                     #
# --------------------------------------------------------------------------- #
def to_mm(value):
    try:
        return int(round(float(value) * MM))
    except (TypeError, ValueError):
        return 0


def to_int(value, default=0):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def format_decimal(mm):
    return f"{mm / MM:g}"


def format_weight(weight):
    return f"{weight:g}"


def sorted_orientations(l, w, h):
    """该尺寸去重后的所有朝向，按确定性顺序排列。"""
    return sorted(set(permutations((l, w, h))))


def original_orientation_order(l, w, h):
    """复现最初版(Decimal 版)的朝向遍历顺序，用于"标准版"以保证逐箱一致。"""
    order = []
    for a, b, c in set(permutations((Decimal(str(l / MM)), Decimal(str(w / MM)), Decimal(str(h / MM))))):
        order.append((int(a * MM), int(b * MM), int(c * MM)))
    return order


def _rect_intersects(a, b):
    """二维极大空矩形算法使用的严格相交判断。"""
    return (a[0] < b[0] + b[2] and a[0] + a[2] > b[0] and
            a[1] < b[1] + b[3] and a[1] + a[3] > b[1])


def _rect_contained(a, b):
    return (a[0] >= b[0] and a[1] >= b[1] and
            a[0] + a[2] <= b[0] + b[2] and
            a[1] + a[3] <= b[1] + b[3])


def _split_free_rectangles(free_rectangles, used):
    """用一个已放矩形切分极大空矩形集合，并删除被包含的冗余区域。"""
    result = []
    for rect in free_rectangles:
        if not _rect_intersects(rect, used):
            result.append(rect)
            continue
        x, y, width, height = rect
        ux, uy, uw, uh = used[:4]
        if ux > x:
            result.append((x, y, ux - x, height))
        if ux + uw < x + width:
            result.append((ux + uw, y, x + width - ux - uw, height))
        if uy > y:
            result.append((x, y, width, uy - y))
        if uy + uh < y + height:
            result.append((x, uy + uh, width, y + height - uy - uh))
    unique = list(dict.fromkeys(r for r in result if r[2] > 0 and r[3] > 0))
    return [rect for i, rect in enumerate(unique)
            if not any(i != j and _rect_contained(rect, other)
                       for j, other in enumerate(unique))]


def _maxrects_pack(container_xy, rectangles, mode='compact'):
    """纯 Python MaxRects 解码器。

    rectangles: [(width, height, payload), ...]；无法放置的矩形会跳过，
    返回 [(x, y, width, height, payload), ...]。
    """
    free = [(0, 0, int(container_xy[0]), int(container_xy[1]))]
    placed = []
    for original_width, original_height, payload in rectangles:
        candidates = []
        for fx, fy, fw, fh in free:
            for width, height in dict.fromkeys(((original_width, original_height),
                                                (original_height, original_width))):
                if width > fw or height > fh:
                    continue
                leftover_h, leftover_w = fh - height, fw - width
                short_side = min(leftover_h, leftover_w)
                long_side = max(leftover_h, leftover_w)
                if mode == 'bssf':
                    key = (short_side, long_side, fy, fx)
                elif mode == 'baf':
                    key = (fw * fh - width * height, short_side, fy, fx)
                elif mode == 'bottom_left':
                    key = (fy + height, fx, short_side)
                else:
                    key = (max(fx + width, fy + height), short_side, long_side, fy, fx)
                candidates.append((key, (fx, fy, width, height, payload)))
        if not candidates:
            continue
        chosen = min(candidates, key=lambda item: item[0])[1]
        placed.append(chosen)
        free = _split_free_rectangles(free, chosen)
    return placed


# --------------------------------------------------------------------------- #
#                            装箱器（整数几何内核）                            #
# --------------------------------------------------------------------------- #
class ContainerPacker:
    def __init__(self, container, boxes, weight_threshold=100,
                 no_flip=None, priority=None, bottom_metric='none', descriptions=None):
        """三维装箱器。

        新增参数：
          no_flip:       每类货物是否「禁止倒放」。可为 list（与 boxes 同序）或 dict{io: bool}。
                         禁止倒放意味着该类货物落地面固定为「长×宽」，高度只能是原始高度 h
                         （运输中不会倒过来，避免内部零件散开）。
          priority:      每类货物的「摆放优先级」。可为 list 或 dict{io: number}；
                         数值越大越优先放到底层。用于「手动模式」。
          bottom_metric: 底层优先指标，决定哪类货物优先放到底层（仍允许叠放）：
                         'none'        —— 不启用，优先最大化空间利用率，其次装入件数
                         'weight'      —— 重量大的优先放底层
                         'volumetric'  —— 体积重(密度=重量/体积)大的优先放底层
                         'manual'      —— 按 priority 手动优先级放底层
          descriptions:  每类货物的「零件描述/品名」。可为 list 或 dict{io: str}，仅用于展示与导出。
        """
        self.container = tuple(int(x) for x in container)
        self.boxes = [
            (int(l), int(w), int(h), int(n), float(weight), t, i)
            for i, (l, w, h, n, weight, t) in enumerate(boxes)
        ]
        self.weight_threshold = float(weight_threshold)
        self.no_flip = self._normalize_flags(no_flip, bool, False)
        self.priority = self._normalize_flags(priority, float, 0.0)
        self.descriptions = self._normalize_flags(
            descriptions, lambda v: '' if v is None else str(v), '')
        self.bottom_metric = bottom_metric if bottom_metric in (
            'none', 'weight', 'volumetric', 'manual') else 'none'
        self.container_volume = self.container[0] * self.container[1] * self.container[2]
        self.total_units = sum(b[3] for b in self.boxes)
        self.cell = max(1, min(self.container) // 15) if self.container and min(self.container) else 1
        self.highlighted_boxes = set()
        self.packing_plan = []
        self.box_colors = []
        self._reset_state()

    # ---------------- 内部状态 ---------------- #
    def _reset_state(self):
        self.occupied = []
        self.grid = defaultdict(list)
        self.top_map = defaultdict(list)
        self.points = {(0, 0, 0)}
        self.packing_plan = []
        self.box_colors = []

    # ---------------- 约束 / 优先级辅助 ---------------- #
    def _normalize_flags(self, value, cast, default):
        """把 no_flip / priority 统一成 {io: value} 字典。"""
        result = {}
        if value is None:
            return result
        if isinstance(value, dict):
            for k, v in value.items():
                try:
                    result[int(k)] = cast(v)
                except (TypeError, ValueError):
                    result[int(k)] = default
        else:  # 按 boxes 顺序给出的序列
            for i, v in enumerate(value):
                try:
                    result[i] = cast(v)
                except (TypeError, ValueError):
                    result[i] = default
        return result

    def _allowed(self, io, orients):
        """按「禁止倒放」过滤朝向：只保留高度(z 向)等于原始高度 h 的朝向。

        禁止倒放 = 落地面固定为长×宽，箱子只能绕竖直轴旋转（长宽可互换），高只能是 h。
        若过滤后为空（理论上不会），则退回全部朝向以保证仍能装箱。
        """
        if self.no_flip.get(io):
            h = self.boxes[io][2]  # 原始高度(mm)
            filtered = [o for o in orients if o[2] == h]
            return filtered or orients
        return orients

    def _sink_key(self, box):
        """底层优先「下沉键」：值越大越应该放到底层（越先放置）。"""
        l, w, h, n, weight, t, io = box
        if self.bottom_metric == 'weight':
            return weight
        if self.bottom_metric == 'volumetric':
            vol = l * w * h  # mm^3
            return (weight * 1e9 / vol) if vol > 0 else 0.0  # 密度 kg/m^3
        if self.bottom_metric == 'manual':
            return self.priority.get(io, 0.0)
        return 0.0

    # ---------------- 空间索引辅助 ---------------- #
    def _cells(self, x, y, z, l, w, h):
        cs = self.cell
        for cx in range(x // cs, (x + l - 1) // cs + 1):
            for cy in range(y // cs, (y + w - 1) // cs + 1):
                for cz in range(z // cs, (z + h - 1) // cs + 1):
                    yield (cx, cy, cz)

    def _neighbors(self, x, y, z, l, w, h):
        result = set()
        for cell in self._cells(x, y, z, l, w, h):
            bucket = self.grid.get(cell)
            if bucket:
                result.update(bucket)
        return result

    def _point_inside_any(self, px, py, pz):
        cs = self.cell
        bucket = self.grid.get((px // cs, py // cs, pz // cs))
        if not bucket:
            return False
        for idx in bucket:
            ox, oy, oz, ol, ow, oh = self.occupied[idx]
            if (ox < px < ox + ol) and (oy < py < oy + ow) and (oz < pz < oz + oh):
                return True
        return False

    # ---------------- 放置可行性 ---------------- #
    def can_place(self, x, y, z, l, w, h, weight):
        if x + l > self.container[0] or y + w > self.container[1] or z + h > self.container[2]:
            return False
        # 说明：重量不再作为「硬约束」阻止叠放。重/密度大的箱子改为通过装箱顺序
        # 优先放到底层（见 _policy_priority 与适应度中的底层得分），实在放不下时仍允许被叠压。
        for idx in self._neighbors(x, y, z, l, w, h):
            ox, oy, oz, ol, ow, oh = self.occupied[idx]
            if (x < ox + ol and x + l > ox and
                    y < oy + ow and y + w > oy and
                    z < oz + oh and z + h > oz):
                return False
        return z == 0 or center_supported(x, y, l, w, self.top_map.get(z, ()))

    def _place(self, x, y, z, l, w, h):
        idx = len(self.occupied)
        box = (x, y, z, l, w, h)
        self.occupied.append(box)
        self.packing_plan.append(box)
        self.top_map[z + h].append((x, y, x + l, y + w))  # 顶面矩形，供支撑判断
        for cell in self._cells(x, y, z, l, w, h):
            self.grid[cell].append(idx)
        if self.points:
            self.points = {
                (px, py, pz) for (px, py, pz) in self.points
                if not (x < px < x + l and y < py < y + w and z < pz < z + h)
            }
        self.points.discard((x, y, z))
        L, W, H = self.container
        for px, py, pz in ((x + l, y, z), (x, y + w, z), (x, y, z + h),
                           (x + l, 0, z), (x + l, y, 0), (0, y + w, z),
                           (x, y + w, 0), (x, 0, z + h), (0, y, z + h)):
            if 0 <= px < L and 0 <= py < W and 0 <= pz < H and not self._point_inside_any(px, py, pz):
                self.points.add((px, py, pz))

    # ---------------- 增强版：竖向列 + 二维排样 ---------------- #
    def _dense_groups(self):
        """合并几何和运输约束相同的类别，同时保留每件货物的原输入类别。"""
        grouped = defaultdict(list)
        for l, w, h, quantity, weight, box_type, io in self.boxes:
            key = (min(l, w), max(l, w), h, bool(self.no_flip.get(io)))
            grouped[key].extend([io] * quantity)
        return [
            {'key': key, 'small': key[0], 'large': key[1], 'height': key[2],
             'units': units, 'count': len(units)}
            for key, units in grouped.items()
        ]

    def _height_column_patterns(self, groups, column_count):
        """在固定数量的主列中，用有界动态规划最大化竖向装载件数。

        最大数量的几何组作为主组；其余组数量通常很少，只把它们放入 DP 状态，
        避免按数百件主货展开成高维状态。所有组的水平投影均能嵌套在主列内。
        """
        if not groups or len(groups) > 3 or column_count <= 0:
            return None
        primary = max(range(len(groups)), key=lambda i: groups[i]['count'])
        ordered_indices = [primary] + [i for i in range(len(groups)) if i != primary]
        ordered = [groups[i] for i in ordered_indices]
        height_limit = self.container[2]
        max_counts = [group['count'] for group in ordered]
        volumes = []
        for group in ordered:
            io = group['units'][0]
            box = self.boxes[io]
            volumes.append(box[0] * box[1] * box[2])

        patterns = []

        def enumerate_counts(index, remaining_height, counts):
            if index == len(ordered):
                if any(counts):
                    patterns.append(tuple(counts))
                return
            height = ordered[index]['height']
            limit = min(max_counts[index], remaining_height // height)
            for quantity in range(limit + 1):
                enumerate_counts(index + 1, remaining_height - quantity * height,
                                 counts + [quantity])

        enumerate_counts(0, height_limit, [])
        # 每列优先体积，其次件数；先尝试密实模式可显著减少 DP 分支。
        patterns.sort(key=lambda counts: (
            sum(counts[i] * volumes[i] for i in range(len(counts))),
            sum(counts)), reverse=True)

        # state(minor counts) -> (primary count, volume, path)
        state = {tuple(0 for _ in ordered[1:]): (0, 0, ())}
        for _ in range(column_count):
            next_state = {}
            for minor_counts, (primary_count, volume, path) in state.items():
                for pattern in patterns:
                    new_primary = primary_count + pattern[0]
                    if new_primary > max_counts[0]:
                        continue
                    new_minor = tuple(minor_counts[i] + pattern[i + 1]
                                      for i in range(len(minor_counts)))
                    if any(new_minor[i] > max_counts[i + 1] for i in range(len(new_minor))):
                        continue
                    new_volume = volume + sum(pattern[i] * volumes[i]
                                              for i in range(len(pattern)))
                    current = next_state.get(new_minor)
                    value = (new_volume, new_primary)
                    if current is None or value > (current[1], current[0]):
                        next_state[new_minor] = (new_primary, new_volume, path + (pattern,))
            state = next_state
            if not state:
                return None

        best = None
        for minor_counts, (primary_count, volume, path) in state.items():
            placed = primary_count + sum(minor_counts)
            value = (volume, placed)
            if best is None or value > best[0]:
                best = (value, path)
        if best is None:
            return None

        result = []
        for pattern in best[1]:
            expanded = []
            # 大投影在下、小投影在上，确保每一层得到充分支撑。
            for ordered_index, quantity in enumerate(pattern):
                original_index = ordered_indices[ordered_index]
                expanded.extend([original_index] * quantity)
            expanded.sort(key=lambda i: (groups[i]['small'] * groups[i]['large'],
                                         groups[i]['small'], groups[i]['large']), reverse=True)
            result.append(expanded)
        return result

    @staticmethod
    def _chunk_rectangles(rectangles, chunk_size, seed):
        """按同尺寸小批交错，兼顾规则成排和类别穿插。"""
        buckets = defaultdict(list)
        for rectangle in rectangles:
            buckets[(rectangle[0], rectangle[1])].append(rectangle)
        chunks = []
        for bucket in buckets.values():
            for start in range(0, len(bucket), chunk_size):
                chunks.append(bucket[start:start + chunk_size])
        random.Random(seed).shuffle(chunks)
        return [rectangle for chunk in chunks for rectangle in chunk]

    def _column_item_orientation(self, io, footprint_width, footprint_height):
        """选择能完全落在列投影内的朝向，优先较大接触面。"""
        l, w, h = self.boxes[io][:3]
        candidates = [orientation for orientation in self._allowed(
            io, sorted_orientations(l, w, h))
            if orientation[0] <= footprint_width and orientation[1] <= footprint_height]
        if not candidates:
            return None
        return max(candidates, key=lambda orientation: (
            orientation[0] * orientation[1], -orientation[2], orientation[0]))

    def _materialize_columns(self, placed_rectangles, groups):
        """把二维列排样展开成逐箱三维坐标，并用正式几何内核复核。"""
        queues = {index: list(group['units']) for index, group in enumerate(groups)}
        self._reset_state()
        number = 1
        type_count = Counter()
        used_volume = 0
        sink_score = 0.0
        container_height = self.container[2]

        for x, y, footprint_width, footprint_height, group_indices in placed_rectangles:
            z = 0
            for group_index in group_indices:
                if not queues[group_index]:
                    continue
                io = queues[group_index].pop(0)
                orientation = self._column_item_orientation(
                    io, footprint_width, footprint_height)
                if orientation is None:
                    return None
                length, width, height = orientation
                px = x + (footprint_width - length) // 2
                py = y + (footprint_height - width) // 2
                weight = self.boxes[io][4]
                if not self.can_place(px, py, z, length, width, height, weight):
                    return None
                self._place(px, py, z, length, width, height)
                l, w, h, quantity, weight, box_type, _ = self.boxes[io]
                type_count[box_type] += 1
                self.box_colors.append({
                    'number': number,
                    'type': box_type,
                    'type_count': type_count[box_type],
                    'weight': weight,
                    'input_order': io,
                    'original_dimensions': (l / MM, w / MM, h / MM),
                    'desc': self.descriptions.get(io, ''),
                })
                used_volume += length * width * height
                sink_score += self._sink_key(self.boxes[io]) * (container_height - z)
                number += 1
                z += height
        return (len(self.packing_plan), used_volume, sink_score,
                list(self.packing_plan), [dict(info) for info in self.box_colors])

    def _dense_column_candidate(self, deadline=None, rng_seed=20260719):
        """列生成 + MaxRects 混合启发式；不适用时返回 None。

        该策略专门补足逐箱极点贪心在高重复、小尺寸种类场景中的短板。它只使用
        完全嵌套的竖向列，因此天然满足不碰撞和重心支撑约束；最终仍由正式内核复核。
        """
        groups = self._dense_groups()
        if len(groups) < 2 or self.total_units < 20:
            return None
        # 当前列高 DP 使用原始高度；可倒放场景继续交给支持六朝向的极点/遗传解码器。
        if any(not group['key'][3] for group in groups):
            return None
        primary_index = max(range(len(groups)), key=lambda i: groups[i]['count'])
        primary = groups[primary_index]
        main_width, main_height = primary['small'], primary['large']
        compatible_indices = [i for i, group in enumerate(groups)
                              if group['small'] <= main_width and group['large'] <= main_height]
        if primary_index not in compatible_indices or len(compatible_indices) > 3:
            return None
        external_indices = [i for i in range(len(groups)) if i not in compatible_indices]
        # 多种互不嵌套的外部投影需要更一般的列选择器；留给极点/遗传策略处理。
        if len(external_indices) > 1:
            return None

        compatible = [groups[i] for i in compatible_indices]
        compatible_remap = {local: original for local, original in enumerate(compatible_indices)}
        primary_local = compatible_indices.index(primary_index)
        if sum(group['count'] for i, group in enumerate(compatible)
               if i != primary_local) > 120:
            return None
        max_primary_per_column = self.container[2] // compatible[primary_local]['height']
        if max_primary_per_column <= 0:
            return None
        minimum_columns = max(1, (primary['count'] + max_primary_per_column - 1) // max_primary_per_column)

        external_columns = []
        if external_indices:
            external_index = external_indices[0]
            external = groups[external_index]
            per_column = self.container[2] // external['height']
            if per_column <= 0:
                return None
            remaining = external['count']
            while remaining > 0:
                quantity = min(per_column, remaining)
                external_columns.append([external_index] * quantity)
                remaining -= quantity
        if minimum_columns + len(external_columns) > 250:
            return None

        best = None
        # 多留几列可改善高度组合；外部列从全量向下尝试，适应几乎满载的地面排样。
        column_counts = [minimum_columns + offset for offset in (2, 1, 3, 0, 4)]
        for column_count in column_counts:
            if deadline is not None and time.time() >= deadline:
                break
            local_patterns = self._height_column_patterns(compatible, column_count)
            if not local_patterns:
                continue
            main_patterns = [[compatible_remap[index] for index in pattern]
                             for pattern in local_patterns]
            main_patterns.sort(key=len, reverse=True)
            min_external = max(0, len(external_columns) - 6)
            for external_count in range(len(external_columns), min_external - 1, -1):
                rectangles = [
                    (main_width, main_height, pattern) for pattern in main_patterns
                ] + [
                    (groups[external_indices[0]]['small'],
                     groups[external_indices[0]]['large'], pattern)
                    for pattern in external_columns[:external_count]
                ] if external_indices else [
                    (main_width, main_height, pattern) for pattern in main_patterns
                ]
                sequences = [rectangles, list(reversed(rectangles))]
                for chunk_size in (12, 8, 4):
                    sequences.append(self._chunk_rectangles(rectangles, chunk_size, 135))
                    sequences.append(self._chunk_rectangles(rectangles, chunk_size, rng_seed))
                for sequence in sequences:
                    for mode in ('bssf', 'compact', 'baf', 'bottom_left'):
                        if deadline is not None and time.time() >= deadline:
                            break
                        layout = _maxrects_pack(self.container[:2], sequence, mode)
                        if len(layout) != len(rectangles):
                            continue
                        candidate = self._materialize_columns(layout, groups)
                        if candidate is None:
                            continue
                        value = self._candidate_fit(candidate)
                        if best is None or value > best[0]:
                            best = (value, candidate)
                    if deadline is not None and time.time() >= deadline:
                        break
        return None if best is None else best[1]

    def _restore_plan(self, plan, colors):
        """恢复候选解，同时重建碰撞/支撑索引，便于后续展示或继续校验。"""
        self._reset_state()
        for placement in plan:
            self._place(*placement)
        self.box_colors = [dict(info) for info in colors]

    # ---------------- 增强版：列模式生成 + 有界束搜索 ---------------- #
    @staticmethod
    def _candidate_fit(candidate):
        """统一候选解的业务目标：先最大化已装体积，再比较件数与底层偏好。"""
        return candidate[1], candidate[0], candidate[2]

    def _cell_orientation(self, group, cell_length, cell_width):
        """选择能放进指定列投影、且占用列高最小的合法朝向。"""
        io = group['representative']
        l, w, h = self.boxes[io][:3]
        candidates = [orientation for orientation in self._allowed(
            io, sorted_orientations(l, w, h))
            if orientation[0] <= cell_length and orientation[1] <= cell_width]
        candidates = [orientation for orientation in candidates
                      if orientation[2] <= self.container[2]]
        if not candidates:
            return None
        return min(candidates, key=lambda orientation: (
            orientation[2], -orientation[0] * orientation[1],
            -min(orientation[0], orientation[1])))

    @staticmethod
    def _column_support_sequence(counts, orientations):
        """把一个列模式展开为自下而上的货物序列。

        列内货物居中放置，上下底面中心重合，天然满足重心支撑规则；
        按投影面积由大到小排列，形成稳定底座。
        """
        sequence = []
        for group_index, quantity in enumerate(counts):
            orientation = orientations[group_index]
            if orientation is not None:
                sequence.extend([(group_index, orientation)] * quantity)
        sequence.sort(key=lambda item: (
            item[1][0] * item[1][1],
            min(item[1][0], item[1][1]),
            max(item[1][0], item[1][1])), reverse=True)
        return sequence

    def _column_patterns_for_cell(self, cell_length, cell_width, groups,
                                  rng_seed=20260719):
        """用多目标有界背包生成一组互补的竖向列模式。

        每种模式都受柜高、合法旋转和重心支撑约束。除体积最密实模式外，
        还保留单类、稀缺类优先和随机扰动模式，供后续束搜索避免局部最优。
        """
        height_limit = self.container[2]
        orientations = [self._cell_orientation(
            group, cell_length, cell_width) for group in groups]
        eligible = [index for index, orientation in enumerate(orientations)
                    if orientation is not None]
        if not eligible:
            return []

        heights = [orientation[2] if orientation is not None else height_limit + 1
                   for orientation in orientations]
        volumes = []
        maximums = []
        for group_index, group in enumerate(groups):
            io = group['representative']
            l, w, h = self.boxes[io][:3]
            volumes.append(l * w * h)
            maximums.append(min(len(group['units']),
                                height_limit // heights[group_index])
                            if orientations[group_index] is not None else 0)

        divisor = height_limit
        for group_index in eligible:
            divisor = math.gcd(divisor, heights[group_index])
        divisor = max(1, divisor)
        scaled_limit = height_limit // divisor
        scaled_heights = [height // divisor for height in heights]
        max_volume = max(volumes[index] for index in eligible)
        base_weights = [volume / max_volume for volume in volumes]
        metrics = [tuple(base_weights), tuple(1.0 for _ in groups)]
        # 单类加权会产生“纯列”和稀缺货物组合，避免体积密度一个指标垄断搜索。
        for focus in eligible:
            for bonus in (1.5, 4.0):
                metrics.append(tuple(
                    base_weights[index] + (bonus if index == focus else 0.0)
                    for index in range(len(groups))))
        rng = random.Random(rng_seed + cell_length * 131 + cell_width * 17)
        for _ in range(18):
            metrics.append(tuple(
                base_weights[index] * rng.uniform(0.55, 2.2)
                for index in range(len(groups))))

        zero = tuple(0 for _ in groups)
        patterns = {zero}

        def solve_knapsack(weights):
            # dp[height] = (score, counts)。每个几何组数量有界，列高用最大公约数缩放。
            dp = [None] * (scaled_limit + 1)
            dp[0] = (0.0, zero)
            for group_index in eligible:
                item_height = scaled_heights[group_index]
                limit = maximums[group_index]
                next_dp = list(dp)
                for used_height, state in enumerate(dp):
                    if state is None:
                        continue
                    max_quantity = min(limit,
                                       (scaled_limit - used_height) // item_height)
                    for quantity in range(1, max_quantity + 1):
                        new_height = used_height + quantity * item_height
                        counts = list(state[1])
                        counts[group_index] += quantity
                        score = state[0] + weights[group_index] * quantity
                        current = next_dp[new_height]
                        if current is None or score > current[0] + 1e-12:
                            next_dp[new_height] = (score, tuple(counts))
                dp = next_dp
            viable = [(state[0], height, state[1])
                      for height, state in enumerate(dp) if state is not None]
            if viable:
                patterns.add(max(viable, key=lambda item: (item[0], item[1]))[2])

        for metric in metrics:
            solve_knapsack(metric)

        # 显式保留每类的最大纯列；这些模式对二维排样和收尾都很重要。
        for group_index in eligible:
            counts = [0] * len(groups)
            counts[group_index] = maximums[group_index]
            patterns.add(tuple(counts))

        # 对当前列中几何、列高和体积完全等价的组展开数量分配。它们在几何上
        # 可以互换，但仍需保留各输入行的数量约束；显式展开可避免 DP 因同分
        # 只留下其中一类，尤其适合相同纸箱来自多个品名/重量行的实际清单。
        equivalent = defaultdict(list)
        for group_index in eligible:
            equivalent[(orientations[group_index], heights[group_index],
                        volumes[group_index])].append(group_index)
        expanded_patterns = set(patterns)
        for counts in list(patterns):
            variants = {counts}
            for indices in equivalent.values():
                if len(indices) < 2:
                    continue
                total_quantity = sum(counts[index] for index in indices)
                if total_quantity <= 0:
                    continue
                allocations = []

                def distribute(position, remaining, allocation):
                    if position == len(indices) - 1:
                        group_index = indices[position]
                        if remaining <= maximums[group_index]:
                            allocations.append(allocation + [remaining])
                        return
                    group_index = indices[position]
                    for quantity in range(min(maximums[group_index], remaining) + 1):
                        distribute(position + 1, remaining - quantity,
                                   allocation + [quantity])

                distribute(0, total_quantity, [])
                next_variants = set()
                for variant in variants:
                    for allocation in allocations:
                        changed = list(variant)
                        for group_index, quantity in zip(indices, allocation):
                            changed[group_index] = quantity
                        next_variants.add(tuple(changed))
                variants = next_variants
            expanded_patterns.update(variants)
        patterns = expanded_patterns

        result = []
        for counts in patterns:
            if not any(counts):
                continue
            sequence = self._column_support_sequence(counts, orientations)
            if sequence is None:
                continue
            used_volume = sum(counts[index] * volumes[index]
                              for index in range(len(groups)))
            used_height = sum(counts[index] * heights[index]
                              for index in range(len(groups)))
            result.append({
                'counts': counts,
                'orientations': tuple(orientations),
                'sequence': tuple(sequence),
                'used': used_volume,
                'placed': sum(counts),
                'height': used_height,
            })
        result.sort(key=lambda pattern: (
            pattern['used'], pattern['placed'], pattern['height']), reverse=True)
        return result

    def _column_floor_layouts(self, groups, deadline=None):
        """生成通用的二维货柜底面列网格候选。

        以数量最多且旋转限制最强的货物作为主列投影，在每一排中枚举主列与
        次级窄列的组合。这等价于小规模的货架式二维排样，不依赖具体客户尺寸。
        """
        if not groups:
            return []
        container_length, container_width, container_height = self.container
        dominant_index = max(
            range(len(groups)),
            key=lambda index: (
                bool(groups[index]['key'][-1]),
                len(groups[index]['units']),
                self.boxes[groups[index]['representative']][0]
                * self.boxes[groups[index]['representative']][1]))
        dominant = groups[dominant_index]
        dominant_io = dominant['representative']
        dominant_box = self.boxes[dominant_io]
        dominant_footprints = sorted(set(
            orientation[:2] for orientation in self._allowed(
                dominant_io, sorted_orientations(*dominant_box[:3]))
            if orientation[0] <= container_length
            and orientation[1] <= container_width))

        all_footprints = set()
        for group in groups:
            io = group['representative']
            box = self.boxes[io]
            for orientation in self._allowed(io, sorted_orientations(*box[:3])):
                if (orientation[0] <= container_length
                        and orientation[1] <= container_width):
                    all_footprints.add(orientation[:2])

        total_cargo_volume = sum(
            self.boxes[group['representative']][0]
            * self.boxes[group['representative']][1]
            * self.boxes[group['representative']][2]
            * len(group['units']) for group in groups)
        candidates = []
        seen = set()

        for dominant_length, row_width in dominant_footprints:
            if deadline is not None and time.time() >= deadline:
                break
            row_count = container_width // row_width
            max_dominant = container_length // dominant_length
            if row_count <= 0 or max_dominant <= 0:
                continue
            secondary_lengths = sorted({
                footprint_length for footprint_length, footprint_width in all_footprints
                if footprint_width <= row_width
                and footprint_length <= dominant_length
            }, reverse=True)
            for dominant_per_row in range(max_dominant, 0, -1):
                used_by_dominant = dominant_per_row * dominant_length
                for secondary_length in secondary_lengths:
                    if secondary_length == dominant_length:
                        secondary_per_row = 0
                    else:
                        secondary_per_row = ((container_length - used_by_dominant)
                                             // secondary_length)
                    key = (dominant_length, row_width, row_count,
                           dominant_per_row, secondary_length, secondary_per_row)
                    if key in seen:
                        continue
                    seen.add(key)
                    cells = []
                    for row in range(row_count):
                        x = 0
                        y = row * row_width
                        for _ in range(dominant_per_row):
                            cells.append((x, y, dominant_length, row_width))
                            x += dominant_length
                        for _ in range(secondary_per_row):
                            cells.append((x, y, secondary_length, row_width))
                            x += secondary_length
                    if not cells or len(cells) > 240:
                        continue
                    prism_volume = sum(length * width * container_height
                                       for _, _, length, width in cells)
                    if prism_volume < total_cargo_volume:
                        continue
                    # 每一类都必须在候选列集合中拥有足够的独立容量；该检查虽是
                    # 乐观上界，但能快速排除明显不可能的底面网格。
                    feasible = True
                    for group in groups:
                        capacity = 0
                        for _, _, length, width in cells:
                            orientation = self._cell_orientation(group, length, width)
                            if orientation is not None:
                                capacity += container_height // orientation[2]
                        if capacity < len(group['units']):
                            feasible = False
                            break
                    if not feasible:
                        continue
                    covered_area = sum(length * width
                                       for _, _, length, width in cells)
                    candidates.append({
                        'uncovered': (container_length * container_width
                                      - covered_area),
                        'excess': prism_volume - total_cargo_volume,
                        'count': len(cells),
                        'secondary': secondary_length,
                        'cells': cells,
                    })

        # 先保留底面覆盖率最高的候选，同时按次级列宽保留多样性。单纯选择
        # “总体积刚好够”的网格容易留下无法再利用的狭长缝隙。
        candidates.sort(key=lambda candidate: (
            candidate['uncovered'], candidate['excess'], candidate['count']))
        selected = candidates[:16]
        by_secondary = defaultdict(list)
        for candidate in candidates:
            by_secondary[candidate['secondary']].append(candidate)
        for family in by_secondary.values():
            selected.extend(family[:2])
        unique = []
        signatures = set()
        for candidate in selected:
            signature = tuple((cell[2], cell[3]) for cell in candidate['cells'])
            if signature in signatures:
                continue
            signatures.add(signature)
            unique.append(candidate)
        unique.sort(key=lambda candidate: (
            candidate['uncovered'], candidate['excess'], candidate['count']))
        return [candidate['cells'] for candidate in unique[:40]]

    def _materialize_column_beam(self, cells, chosen_patterns, groups):
        """把束搜索得到的列模式展开为逐箱坐标，并交给正式几何内核复核。"""
        queues = {index: list(group['units']) for index, group in enumerate(groups)}
        self._reset_state()
        number = 1
        type_count = Counter()
        used_volume = 0
        sink_score = 0.0
        container_height = self.container[2]

        for (x, y, cell_length, cell_width), pattern in zip(cells, chosen_patterns):
            z = 0
            for group_index, orientation in pattern['sequence']:
                if not queues[group_index]:
                    return None
                io = queues[group_index].pop(0)
                length, width, height = orientation
                px = x + (cell_length - length) // 2
                py = y + (cell_width - width) // 2
                weight = self.boxes[io][4]
                position = (px, py, z, length, width, height)
                if not self.can_place(*position, weight):
                    return None
                self._place(*position)
                l, w, h, quantity, weight, box_type, _ = self.boxes[io]
                type_count[box_type] += 1
                self.box_colors.append({
                    'number': number,
                    'type': box_type,
                    'type_count': type_count[box_type],
                    'weight': weight,
                    'input_order': io,
                    'original_dimensions': (l / MM, w / MM, h / MM),
                    'desc': self.descriptions.get(io, ''),
                })
                used_volume += length * width * height
                sink_score += self._sink_key(self.boxes[io]) * (container_height - z)
                number += 1
                z += height
        return (len(self.packing_plan), used_volume, sink_score,
                list(self.packing_plan), [dict(info) for info in self.box_colors])

    def _column_beam_candidate(self, deadline=None, rng_seed=20260719,
                               beam_width=320):
        """列模式生成、二维排样与有界束搜索组成的通用候选。

        状态按已装体积优先排序，并以剩余列对各货物的理论容量作为上界剪枝。
        相同剩余数量的状态会合并（记忆化），在限定时间内持续改善可行解。
        """
        if self.total_units < 20:
            return None
        groups = self._column_layer_groups()
        layouts = self._column_floor_layouts(groups, deadline=deadline)
        if not layouts:
            return None
        group_volumes = []
        initial_remaining = []
        for group in groups:
            io = group['representative']
            l, w, h = self.boxes[io][:3]
            group_volumes.append(l * w * h)
            initial_remaining.append(len(group['units']))
        initial_remaining = tuple(initial_remaining)
        total_cargo_volume = sum(
            initial_remaining[index] * group_volumes[index]
            for index in range(len(groups)))
        pattern_cache = {}
        best_candidate = None

        for layout_index, raw_cells in enumerate(layouts):
            if deadline is not None and time.time() >= deadline:
                break
            # 先处理窄列：它们可接纳的货物更少，能更早暴露不可行分支。
            cells = sorted(raw_cells, key=lambda cell: (
                cell[2] * cell[3], cell[2], cell[3], cell[1], cell[0]))
            patterns_by_cell = []
            for cell in cells:
                cell_type = (cell[2], cell[3])
                if cell_type not in pattern_cache:
                    pattern_cache[cell_type] = self._column_patterns_for_cell(
                        cell_type[0], cell_type[1], groups,
                        rng_seed + layout_index * 1009)
                patterns_by_cell.append(pattern_cache[cell_type])
            if any(not patterns for patterns in patterns_by_cell):
                continue

            # suffix_capacity[i][g]：第 i 列及其后对第 g 组货物的乐观件数上界。
            suffix_capacity = [[0] * len(groups) for _ in range(len(cells) + 1)]
            for cell_index in range(len(cells) - 1, -1, -1):
                suffix_capacity[cell_index] = list(suffix_capacity[cell_index + 1])
                _, _, cell_length, cell_width = cells[cell_index]
                for group_index, group in enumerate(groups):
                    orientation = self._cell_orientation(
                        group, cell_length, cell_width)
                    if orientation is not None:
                        suffix_capacity[cell_index][group_index] += (
                            self.container[2] // orientation[2])

            empty_pattern = {
                'counts': tuple(0 for _ in groups),
                'sequence': (),
                'used': 0,
                'placed': 0,
            }
            # state = (remaining, chosen_patterns, used_volume, placed_count)
            beam = [(initial_remaining, (), 0, 0)]
            for cell_index, patterns in enumerate(patterns_by_cell):
                if deadline is not None and time.time() >= deadline:
                    break
                next_by_remaining = {}
                choices = patterns + [empty_pattern]
                for state_index, (remaining, chosen, used, placed) in enumerate(beam):
                    if (deadline is not None and state_index % 32 == 0
                            and time.time() >= deadline):
                        break
                    for pattern in choices:
                        counts = pattern['counts']
                        if any(counts[index] > remaining[index]
                               for index in range(len(groups))):
                            continue
                        new_remaining = tuple(
                            remaining[index] - counts[index]
                            for index in range(len(groups)))
                        candidate_state = (
                            new_remaining,
                            chosen + (pattern,),
                            used + pattern['used'],
                            placed + pattern['placed'],
                        )
                        current = next_by_remaining.get(new_remaining)
                        if current is None or candidate_state[2:] > current[2:]:
                            next_by_remaining[new_remaining] = candidate_state
                if not next_by_remaining:
                    break

                future_capacity = suffix_capacity[cell_index + 1]

                def state_score(state):
                    remaining, _, used, placed = state
                    stranded_volume = sum(
                        max(0, remaining[index] - future_capacity[index])
                        * group_volumes[index]
                        for index in range(len(groups)))
                    optimistic = total_cargo_volume - stranded_volume
                    return optimistic, used, placed, -sum(remaining)

                ranked = sorted(next_by_remaining.values(),
                                key=state_score, reverse=True)
                beam = ranked[:beam_width]
                complete = next((state for state in beam
                                 if not any(state[0])), None)
                if complete is not None:
                    remaining, chosen, used, placed = complete
                    # 补齐尚未处理的空列，方便与坐标一一对应。
                    chosen = chosen + (empty_pattern,) * (len(cells) - len(chosen))
                    candidate = self._materialize_column_beam(cells, chosen, groups)
                    if candidate is not None:
                        return candidate

            if not beam:
                continue
            best_state = max(beam, key=lambda state: (state[2], state[3]))
            chosen = best_state[1] + (empty_pattern,) * (len(cells) - len(best_state[1]))
            candidate = self._materialize_column_beam(cells, chosen, groups)
            if candidate is None:
                continue
            if (best_candidate is None
                    or self._candidate_fit(candidate) > self._candidate_fit(best_candidate)):
                best_candidate = candidate
        return best_candidate

    # ---------------- 增强版：通用列底座 + 顶层续装 ---------------- #
    def _column_layer_groups(self):
        """合并旋转后几何等价的货物，保留每一件的原输入类别。

        可倒放货物按三边排序后的尺寸合并；禁止倒放货物只合并长宽可互换且
        原始高度相同的类别。这样搜索维度与货物种类数相关，而不是与件数相关。
        """
        grouped = {}
        for l, w, h, quantity, weight, box_type, io in self.boxes:
            if self.no_flip.get(io):
                key = (min(l, w), max(l, w), h, True)
            else:
                key = (*sorted((l, w, h)), False)
            if key not in grouped:
                grouped[key] = {'key': key, 'units': [], 'representative': io}
            grouped[key]['units'].extend([io] * quantity)
        return list(grouped.values())

    def _best_vertical_orientation(self, group):
        """选择单位货物占用地面最少、同时列高利用率高的竖向朝向。"""
        io = group['representative']
        l, w, h = self.boxes[io][:3]
        candidates = []
        for orientation in self._allowed(io, sorted_orientations(l, w, h)):
            capacity = self.container[2] // orientation[2]
            if capacity <= 0:
                continue
            area = orientation[0] * orientation[1]
            candidates.append(((area / capacity,
                                self.container[2] - capacity * orientation[2],
                                area, orientation[2]), orientation, capacity))
        if not candidates:
            return None
        _, orientation, capacity = min(candidates, key=lambda item: item[0])
        return orientation, capacity

    @staticmethod
    def _column_layer_sequences(rectangles, seed):
        """为二维列底座生成少量确定性/分块交错序列。"""
        sequences = [
            sorted(rectangles,
                   key=lambda rectangle: (rectangle[0] * rectangle[1],
                                          max(rectangle[0], rectangle[1])),
                   reverse=True),
            sorted(rectangles,
                   key=lambda rectangle: (max(rectangle[0], rectangle[1]),
                                          rectangle[0] * rectangle[1]),
                   reverse=True),
        ]
        for chunk_size in (8, 4, 2):
            sequences.append(ContainerPacker._chunk_rectangles(
                rectangles, chunk_size, seed + chunk_size))
        return sequences

    def _materialize_layer_columns(self, layout, remainder_units):
        """把二维列底座展开，再用相同硬约束内核把余货续装到列顶。"""
        self._reset_state()
        number = 1
        type_count = Counter()
        used_volume = 0
        sink_score = 0.0
        container_height = self.container[2]

        def add_unit(io, position):
            nonlocal number, used_volume, sink_score
            l, w, h, quantity, weight, box_type, _ = self.boxes[io]
            self._place(*position)
            type_count[box_type] += 1
            self.box_colors.append({
                'number': number,
                'type': box_type,
                'type_count': type_count[box_type],
                'weight': weight,
                'input_order': io,
                'original_dimensions': (l / MM, w / MM, h / MM),
                'desc': self.descriptions.get(io, ''),
            })
            used_volume += position[3] * position[4] * position[5]
            sink_score += self._sink_key(self.boxes[io]) * (container_height - position[2])
            number += 1

        for x, y, footprint_length, footprint_width, payload in layout:
            orientation, units = payload
            length, width, height = orientation
            if (footprint_length, footprint_width) == (width, length):
                length, width = width, length
            if (length, width) != (footprint_length, footprint_width):
                return None
            z = 0
            for io in units:
                weight = self.boxes[io][4]
                position = (x, y, z, length, width, height)
                if not self.can_place(*position, weight):
                    return None
                add_unit(io, position)
                z += height

        # 低高度/大接触面朝向优先，适合利用密集列形成的平整顶面。所有位置仍由
        # can_place 复核边界、碰撞和重心支撑，不引入任何样例专用几何假设。
        for io in remainder_units:
            l, w, h = self.boxes[io][:3]
            orientations = self._allowed(io, sorted_orientations(l, w, h))
            orientations = sorted(
                orientations,
                key=lambda orientation: (orientation[2],
                                         -orientation[0] * orientation[1],
                                         orientation[1], orientation[0]))
            chosen = None
            for x, y, z in sorted(self.points, key=lambda point: (point[2], point[1], point[0])):
                for length, width, height in orientations:
                    if self.can_place(x, y, z, length, width, height,
                                      self.boxes[io][4]):
                        chosen = (x, y, z, length, width, height)
                        break
                if chosen is not None:
                    break
            if chosen is None:
                break
            add_unit(io, chosen)

        return (len(self.packing_plan), used_volume, sink_score,
                list(self.packing_plan), [dict(info) for info in self.box_colors])

    def _layer_column_candidate(self, deadline=None, rng_seed=20260719):
        """通用“列底座 + 顶层续装”候选。

        对每个几何组轮流保留一部分货物作为顶层余货，其余类型先组成竖向密实列；
        列投影用 MaxRects 排样，再由三维极点内核把余货续装到列顶。该方法对尺寸、
        数量和类别均无硬编码，尤其能补足逐类贪心无法回收大面积顶部空间的短板。
        """
        if self.total_units < 20:
            return None
        groups = self._column_layer_groups()
        if len(groups) < 2:
            return None
        orientations = []
        for group in groups:
            selected = self._best_vertical_orientation(group)
            if selected is None:
                return None
            orientations.append(selected)

        # 优先尝试“竖放与平放高度差”最大的组：它最可能以密实列放一部分、
        # 再以低矮层覆盖其他列的顶面。该排序只依赖几何比例。
        overlay_order = sorted(
            range(len(groups)),
            key=lambda index: (
                orientations[index][0][2] /
                min(orientation[2] for orientation in self._allowed(
                    groups[index]['representative'],
                    sorted_orientations(*self.boxes[groups[index]['representative']][:3]))),
                len(groups[index]['units'])),
            reverse=True)
        best = None
        total = self.total_units
        ratios = (0.5, 0.4, 0.6, 0.3, 0.7, 0.8)

        for overlay_index in overlay_order:
            for ratio in ratios:
                if deadline is not None and time.time() >= deadline:
                    return None if best is None else best[1]
                rectangles = []
                remainder_units = []
                for group_index, group in enumerate(groups):
                    orientation, capacity = orientations[group_index]
                    units = group['units']
                    maximum_columns = (len(units) + capacity - 1) // capacity
                    if group_index == overlay_index:
                        column_count = max(1, int(maximum_columns * ratio))
                    else:
                        column_count = maximum_columns
                    base_count = min(len(units), column_count * capacity)
                    for start in range(0, base_count, capacity):
                        column_units = units[start:min(start + capacity, base_count)]
                        rectangles.append((orientation[0], orientation[1],
                                           (orientation, column_units)))
                    remainder_units.extend(units[base_count:])

                sequences = self._column_layer_sequences(
                    rectangles, rng_seed + overlay_index * 101 + int(ratio * 100))
                for sequence in sequences:
                    # compact 首选能同时控制两个方向的外包络；其余准则用于处理
                    # 不同长宽比组合，属于通用二维排样重启。
                    for mode in ('compact', 'bssf', 'baf', 'bottom_left'):
                        if deadline is not None and time.time() >= deadline:
                            return None if best is None else best[1]
                        layout = _maxrects_pack(self.container[:2], sequence, mode)
                        if len(layout) != len(rectangles):
                            continue
                        candidate = self._materialize_layer_columns(layout, remainder_units)
                        if candidate is None:
                            continue
                        value = self._candidate_fit(candidate)
                        if best is None or value > best[0]:
                            best = (value, candidate)
                        if candidate[0] == total:
                            return candidate
        return None if best is None else best[1]

    # ---------------- 块构建束搜索（block_engine） ---------------- #
    def _block_kinds(self):
        """按允许朝向合并几何相同的类别；每类的件按「下沉键」从大到小排，重货先放。"""
        grouped = {}
        for l, w, h, quantity, weight, box_type, io in self.boxes:
            if quantity <= 0:
                continue
            orientations = tuple(self._allowed(io, sorted_orientations(l, w, h)))
            grouped.setdefault(orientations, []).extend([io] * quantity)
        kinds = []
        for orientations, units in grouped.items():
            units.sort(key=lambda io: -self._sink_key(self.boxes[io]))
            kinds.append((list(orientations), units))
        return kinds

    def _materialize_blocks(self, placements, kinds):
        """把块的放置记录展开成逐箱坐标，并由正式几何内核逐件复核。"""
        queues = [list(units) for _, units in kinds]
        cursor = [0] * len(kinds)
        self._reset_state()
        type_count = Counter()
        used_volume, sink_score = 0, 0.0
        container_height = self.container[2]
        boxes = []
        for x, y, z, comps in placements:
            # 组合块内先放下层子块，再放叠在上面的子块；子块内自下而上
            for dx, dy, dz, a, b, c, nx, ny, nz, kind in sorted(comps, key=lambda comp: comp[2]):
                for kz in range(nz):
                    for ix in range(nx):
                        for jy in range(ny):
                            boxes.append((kind, (x + dx + ix * a, y + dy + jy * b,
                                                 z + dz + kz * c, a, b, c)))
        for kind, position in boxes:
            io = queues[kind][cursor[kind]]
            cursor[kind] += 1
            l, w, h, quantity, weight, box_type, _ = self.boxes[io]
            if not self.can_place(*position, weight):
                return None
            self._place(*position)
            type_count[box_type] += 1
            self.box_colors.append({
                'number': len(self.box_colors) + 1,
                'type': box_type,
                'type_count': type_count[box_type],
                'weight': weight,
                'input_order': io,
                'original_dimensions': (l / MM, w / MM, h / MM),
                'desc': self.descriptions.get(io, ''),
            })
            used_volume += position[3] * position[4] * position[5]
            sink_score += self._sink_key(self.boxes[io]) * (container_height - position[2])
        return (len(self.packing_plan), used_volume, sink_score,
                list(self.packing_plan), [dict(info) for info in self.box_colors])

    def _block_candidate(self, deadline=None, general=False, max_evaluations=None):
        """块构建候选：只做一次贪心，或束搜索到 deadline / 达到 max_evaluations 次补全为止。

        general=True 时同时使用组合块（不同货物拼成的块）。
        返回 (候选解, 统计)；候选解格式与其他直接生成的策略一致。
        """
        kinds = self._block_kinds()
        if not kinds:
            return None, {}
        search = BlockSearch(self.container, [(orientations, len(units)) for orientations, units in kinds],
                             general=general)
        greedy = best = search.greedy()
        width = 0
        has_budget = (deadline is not None and time.time() < deadline) or max_evaluations
        if has_budget and best.volume < search.total_volume:
            best, width = search.beam_search(deadline, best, max_evaluations)
        candidate = self._materialize_blocks(best.placements(), kinds)
        if candidate is None and best is not greedy:
            # 理论上不会发生：展开复核失败时退回贪心方案，避免整个候选作废
            candidate = self._materialize_blocks(greedy.placements(), kinds)
        return candidate, {'evaluations': search.evaluations, 'beam_width': width}

    # ---------------- 单个策略的装箱 ---------------- #
    def _run_policy(self, ordered_types, orient_map, record=True):
        """按给定"类别顺序 + 每类朝向顺序"装箱。

        ordered_types: [(l,w,h,n,weight,t,input_order), ...] 按放置先后排列
        orient_map:    {input_order: [orientation(mm三元组), ...]}
        装不下的单件会被【跳过】并继续装后面的货物，以最大化装载体积；
        同一类中若有一件放不下，其余同尺寸同类件也必然放不下，直接跳过以节省时间。
        返回 (是否全部装入, 已装数量, 已用体积, 底层优先得分)

        底层优先得分 sink_score = Σ 下沉键 ×(箱底距柜顶的高度)：重/密度大/高优先级的箱子
        放得越低，得分越高。它只作为「装载件数与利用率相同」时的次级排序依据，
        因此启用底层优先【绝不会】减少能装下的件数（软偏好，非硬约束）。
        """
        self._reset_state()
        number = 1
        type_count = {}
        used_volume = 0
        sink_score = 0.0
        H = self.container[2]  # 柜高（z 向，不受长宽对调影响）
        all_placed = True
        for (l, w, h, n, weight, t, io) in ordered_types:
            type_count[t] = type_count.get(t, 0) + 1
            orients = orient_map[io]
            sk = self._sink_key((l, w, h, n, weight, t, io))  # 该类下沉键
            exhausted = False
            for _ in range(n):
                best = None
                if not exhausted:
                    for (px, py, pz) in sorted(self.points, key=lambda p: (p[2], p[1], p[0])):
                        for (bl, bw, bh) in orients:
                            if self.can_place(px, py, pz, bl, bw, bh, weight):
                                best = (px, py, pz, bl, bw, bh)
                                break
                        if best:
                            break
                if best is None:
                    all_placed = False
                    exhausted = True
                    continue
                self._place(*best)
                used_volume += best[3] * best[4] * best[5]
                sink_score += sk * (H - best[2])  # best[2]=箱底 z，越低得分越高
                if record:
                    self.box_colors.append({
                        'number': number,
                        'type': t,
                        'type_count': type_count[t],
                        'weight': weight,
                        'input_order': io,
                        'original_dimensions': (l / MM, w / MM, h / MM),
                        'desc': self.descriptions.get(io, ''),
                    })
                number += 1
        return all_placed, len(self.packing_plan), used_volume, sink_score

    def _solve(self, swap_xy, ordered_types, orient_map, record=True):
        """在（可选长宽对调的）坐标系中求解，再把结果映射回原坐标系。

        长宽对调只是换个观察轴，物理上是同一个集装箱，但会显著影响贪心结果，
        因此两种朝向都尝试、取更优者。
        """
        original = self.container
        if swap_xy:
            self.container = (original[1], original[0], original[2])
        try:
            ok, placed, used, sink = self._run_policy(ordered_types, orient_map, record=record)
        finally:
            self.container = original
        if swap_xy:
            self.packing_plan = [(y, x, z, w, l, h) for (x, y, z, l, w, h) in self.packing_plan]
        return ok, placed, used, sink

    def _deterministic_policies(self):
        """一组确定性策略（不同排序 / 朝向来源），用于组合搜索。"""
        pols = [
            self._policy_original(),
            self._policy_sorted(lambda b: (max(b[0], b[1], b[2]), b[0] * b[1] * b[2])),
            self._policy_sorted(lambda b: (b[0] * b[1] * b[2], max(b[0], b[1], b[2]))),
            self._policy_sorted(lambda b: (b[0] * b[1], b[2])),
            self._policy_sorted(lambda b: (b[2], b[0] * b[1])),
        ]
        # 启用底层优先时，额外提供一个「按下沉键排序」的候选策略：
        # 它把重/密度大/高优先级的箱子排在前面（先放→更靠底层）。
        # 是否采用仍由适应度 (利用率, 件数, 底层得分) 决定，故不会牺牲装载体积。
        if self.bottom_metric != 'none':
            pols.insert(0, self._policy_priority())
        return pols

    # ---------------- 策略构造 ---------------- #
    def _policy_original(self):
        ordered = sorted(self.boxes, key=lambda b: (b[0] * b[1] * b[2], max(b[0], b[1], b[2])), reverse=True)
        orient_map = {b[6]: self._allowed(b[6], original_orientation_order(b[0], b[1], b[2]))
                      for b in self.boxes}
        return ordered, orient_map

    def _policy_sorted(self, key):
        ordered = sorted(self.boxes, key=key, reverse=True)
        orient_map = {b[6]: self._allowed(b[6], sorted_orientations(b[0], b[1], b[2]))
                      for b in self.boxes}
        return ordered, orient_map

    def _policy_priority(self):
        """底层优先候选策略：按「下沉键」(重量/密度/手动优先级) 从大到小排序，
        同级再按体积从大到小，让需要放底层的货物尽量先放。"""
        ordered = sorted(self.boxes,
                         key=lambda b: (self._sink_key(b), b[0] * b[1] * b[2]), reverse=True)
        orient_map = {b[6]: self._allowed(b[6], sorted_orientations(b[0], b[1], b[2]))
                      for b in self.boxes}
        return ordered, orient_map

    def _policy_from_genome(self, type_perm, orient_genes):
        """遗传基因 -> 装箱策略。

        type_perm:    self.boxes 下标的一个排列（决定各类别放置先后）
        orient_genes: {input_order: 旋转量}  决定该类别最先尝试哪个朝向
        """
        ordered = [self.boxes[i] for i in type_perm]
        orient_map = {}
        for b in self.boxes:
            base = self._allowed(b[6], sorted_orientations(b[0], b[1], b[2]))
            g = orient_genes.get(b[6], 0) % len(base)
            orient_map[b[6]] = base[g:] + base[:g]
        return ordered, orient_map

    # ---------------- 对外：标准版 ---------------- #
    # 标准版块构建束搜索的补全次数上限：按次数而非时间封顶，同样输入结果完全一致
    STANDARD_BEAM_EVALUATIONS = 100

    def pack(self):
        """标准版：块构建（贪心 + 短束搜索 + 组合块贪心）与原有逐箱策略取更优者。

        依次尝试，任何一步已把货物全部装入就立即返回；都装不下时再运行原有的
        确定性逐箱策略（长宽两种朝向 × 多种排序）作为下界，取体积最大的方案。
        全程不按时间截断，同样的输入每次结果相同。
        返回 (是否全部装入, 说明)。
        """
        total = self.total_units
        if self.bottom_metric == 'manual':
            return self._pack_manual(total)  # 手动模式：严格按用户优先级摆放
        block = None
        for options in ({}, {'general': True}):
            candidate, _ = self._block_candidate(
                max_evaluations=self.STANDARD_BEAM_EVALUATIONS if not options else None, **options)
            if candidate is not None and (block is None
                                          or self._candidate_fit(candidate) > self._candidate_fit(block)):
                block = candidate
            if block is not None and block[0] == total:
                break
        best_fit, best = None, None
        if block is not None:
            best_fit = self._candidate_fit(block)
        if best_fit is None or best_fit[1] < total:
            for swap in (False, True):
                for pol in self._deterministic_policies():
                    _, placed, used, sink = self._solve(swap, pol[0], pol[1], record=False)
                    # 适应度：体积优先，其次件数，最后用底层得分做同分排序（软偏好）
                    fit = (used, placed, sink)
                    if best_fit is None or fit > best_fit:
                        best_fit, best = fit, (swap, pol)
                    if placed == total:
                        break  # 已满载即提前结束（底层优先策略排在最前，会被优先尝试）
                if best_fit[1] == total:
                    break
        if best is None:
            self._restore_plan(block[3], block[4])
            placed, used = block[0], block[1]
        else:
            _, placed, used, sink = self._solve(best[0], best[1][0], best[1][1], record=True)
        if placed == total:
            return True, "成功"
        return False, (f"已尽最大努力装载：装入 {placed}/{total} 件，"
                       f"空间利用率 {used / self.container_volume * 100:.1f}%（其余货物放不下）")

    # ---------------- 对外：手动优先级 ---------------- #
    def _pack_manual(self, total):
        """手动模式：严格按用户设定的优先级（高优先级先放→更靠底层）装箱。

        这是用户主动选择的强约束，因此以「尊重优先级」为首要目标；仍会在两种长宽
        朝向中选装载更多的一种，尽量少丢件。若无法全部装入，则据实提示（需求5）。
        """
        pol = self._policy_priority()
        best_fit, best_swap = None, False
        for swap in (False, True):
            _, placed, used, _ = self._solve(swap, pol[0], pol[1], record=False)
            fit = (placed, used)  # 手动模式优先保证装载件数，其次利用率
            if best_fit is None or fit > best_fit:
                best_fit, best_swap = fit, swap
        _, placed, used, _ = self._solve(best_swap, pol[0], pol[1], record=True)
        if placed == total:
            return True, "成功（已按您设定的优先级摆放，高优先级货物优先放底层）"
        return False, (f"已按您设定的优先级尽力装载：装入 {placed}/{total} 件，"
                       f"空间利用率 {used / self.container_volume * 100:.1f}%（其余货物放不下）")

    # ---------------- 对外：增强版（块构建束搜索 / 列模式 / MaxRects / 遗传搜索） ---------------- #
    def pack_enhanced(self, time_budget=600.0, rng_seed=20260719,
                      population=24, verbose=True):
        """增强版：块构建束搜索为主，列模式、MaxRects 与遗传搜索为辅，保证不劣于标准版。

        time_budget: 搜索时间上限（秒）；全部货物装入后会提前结束。
        返回 (是否装入, 说明, 统计信息 dict)。
        """
        total = self.total_units
        if self.bottom_metric == 'manual':
            # 手动模式的摆放顺序由用户优先级确定，无需搜索；直接按优先级装箱
            fits, msg = self._pack_manual(total)
            used = sum(l * w * h for _, _, _, l, w, h in self.packing_plan)
            stats = {'placed': len(self.packing_plan), 'total': total,
                     'utilization': used / self.container_volume if self.container_volume else 0.0,
                     'evaluations': 0, 'seconds': 0.0,
                     'optimal_full': fits}
            return fits, msg, stats
        num_types = len(self.boxes)
        rng = random.Random(rng_seed)
        t0 = time.time()
        evals = 0
        best_fit = None
        best = None  # (swap_xy, (ordered_types, orient_map))
        best_plan = None  # 混合启发式直接产生的 (plan, colors)
        best_strategy = None

        # --- 评估：适应度 = (已装体积, 已装件数, 底层得分)。空间利用率是
        #     首要业务目标；底层得分只作最终同分项。---
        def ev(swap, pol):
            nonlocal evals, best_fit, best, best_plan, best_strategy
            _, placed, used, sink = self._solve(swap, pol[0], pol[1], record=False)
            evals += 1
            fit = (used, placed, sink)
            if best_fit is None or fit > best_fit:
                best_fit, best = fit, (swap, pol)
                best_plan = None
                best_strategy = 'extreme_point_search'
            return fit

        def consider_direct(candidate, strategy):
            """接纳直接生成的候选；精英比较保证新算法绝不覆盖更优旧解。"""
            nonlocal evals, best_fit, best_plan, best_strategy
            if candidate is None:
                return
            placed, used, sink, plan, colors = candidate
            evals += 1
            fit = (used, placed, sink)
            if best_fit is None or fit > best_fit:
                best_fit = fit
                best_plan = (plan, colors)
                best_strategy = strategy

        def complete():
            return best_fit is not None and best_fit[1] >= total

        beam_info = {}
        # --- 块构建贪心：通常已很紧密，耗时极少，先作为起点。---
        block, info = self._block_candidate()
        evals += info.get('evaluations', 0)
        consider_direct(block, 'block_greedy')

        # 短预算沿用原有顺序，避免兼容策略因标准种子耗时而失去完整搜索窗口。
        if (time_budget < 30 and not complete()
                and any(not self.no_flip.get(box[6]) for box in self.boxes)):
            layered = self._layer_column_candidate(
                deadline=t0 + time_budget, rng_seed=rng_seed)
            consider_direct(layered, 'layer_columns')

        # --- 块构建束搜索：主力搜索，占预算前 55%；束宽逐轮加倍，时间越长结果越好。---
        if not complete() and time.time() - t0 < time_budget * 0.55:
            beam, beam_info = self._block_candidate(deadline=t0 + time_budget * 0.55)
            evals += beam_info.get('evaluations', 0)
            consider_direct(beam, 'block_beam_search')

        # --- 组合块贪心：放在简单块束搜索之后，不挤占它的时间份额。---
        if not complete():
            block, info = self._block_candidate(general=True)
            evals += info.get('evaluations', 0)
            consider_direct(block, 'general_block_greedy')

        # 列模式束搜索擅长「少种类、高重复」的清单；若它直接装完即可结束。
        if time_budget >= 30 and not complete():
            beam_share = 0.7
            column_beam = self._column_beam_candidate(
                deadline=t0 + time_budget * beam_share,
                rng_seed=rng_seed, beam_width=420)
            consider_direct(column_beam, 'column_beam_search')

        # --- 若尚未全装，评估标准版全部种子，建立体积利用率精英下界。无论
        #     其他搜索是否超时，增强版都不会返回比标准版更差的方案。---
        if not complete():
            for swap in (False, True):
                for pol in self._deterministic_policies():
                    ev(swap, pol)
                    if complete():
                        break
                if complete():
                    break

        # --- 旧的列底座 + 顶层续装仍作为互补精英策略保留。---
        if (not complete() and time_budget >= 30
                and time.time() - t0 < time_budget * 0.85
                and any(not self.no_flip.get(box[6]) for box in self.boxes)):
            layered = self._layer_column_candidate(
                deadline=t0 + time_budget * 0.85, rng_seed=rng_seed)
            consider_direct(layered, 'layer_columns')

        # --- 禁止倒放且高重复的专用列生成策略。---
        if not complete() and time.time() - t0 < time_budget * 0.85:
            dense = self._dense_column_candidate(deadline=t0 + time_budget * 0.85,
                                                 rng_seed=rng_seed)
            consider_direct(dense, 'dense_columns')

        # --- 组合块束搜索：不同货物拼成的块，对种类多、每种件数少的清单更有效。
        #     使用原先留给遗传搜索的时间，不占用上面简单块束搜索的份额。---
        if not complete() and time.time() - t0 < time_budget * 0.97:
            general, general_info = self._block_candidate(
                deadline=t0 + time_budget * 0.97, general=True)
            evals += general_info.get('evaluations', 0)
            consider_direct(general, 'general_block_beam_search')

        # --- 遗传搜索（种子未达 100% 时启动；长宽朝向也作为基因参与进化）---
        if not complete():

            def random_genome():
                perm = list(range(num_types))
                rng.shuffle(perm)
                genes = {b[6]: rng.randrange(6) for b in self.boxes}
                return (rng.random() < 0.5, perm, genes)

            def genome_fit(g):
                return ev(g[0], self._policy_from_genome(g[1], g[2]))

            def crossover(a, b):
                n = num_types
                if n >= 2:
                    i, j = sorted(rng.sample(range(n), 2))
                else:
                    i = j = 0
                child_perm = [None] * n
                child_perm[i:j + 1] = a[1][i:j + 1]
                fill = [x for x in b[1] if x not in child_perm]
                k = 0
                for p in range(n):
                    if child_perm[p] is None:
                        child_perm[p] = fill[k]
                        k += 1
                genes = {bx[6]: (a[2][bx[6]] if rng.random() < 0.5 else b[2][bx[6]]) for bx in self.boxes}
                return (a[0] if rng.random() < 0.5 else b[0], child_perm, genes)

            def mutate(g):
                swap, perm, genes = g[0], list(g[1]), dict(g[2])
                if rng.random() < 0.25:
                    swap = not swap
                if rng.random() < 0.7 and num_types >= 2:
                    i, j = rng.sample(range(num_types), 2)
                    perm[i], perm[j] = perm[j], perm[i]
                if rng.random() < 0.7:
                    bx = self.boxes[rng.randrange(num_types)]
                    genes[bx[6]] = rng.randrange(6)
                return (swap, perm, genes)

            pop = []
            while len(pop) < population and not complete() and time.time() - t0 < time_budget:
                g = random_genome()
                pop.append((genome_fit(g), g))

            while pop and not complete() and time.time() - t0 < time_budget:
                pop.sort(key=lambda it: it[0], reverse=True)
                new_pop = pop[:max(2, population // 5)]
                while len(new_pop) < population and time.time() - t0 < time_budget:
                    p1 = max(rng.sample(pop, min(3, len(pop))), key=lambda it: it[0])[1]
                    p2 = max(rng.sample(pop, min(3, len(pop))), key=lambda it: it[0])[1]
                    child = mutate(crossover(p1, p2)) if rng.random() < 0.9 else random_genome()
                    new_pop.append((genome_fit(child), child))
                pop = new_pop

        # --- 用最优策略正式装箱（生成编号/颜色信息） ---
        if best_plan is not None:
            self._restore_plan(best_plan[0], best_plan[1])
            placed = len(self.packing_plan)
            used = sum(length * width * height
                       for _, _, _, length, width, height in self.packing_plan)
        else:
            _, placed, used, sink = self._solve(best[0], best[1][0], best[1][1], record=True)
        stats = {
            'placed': placed,
            'total': total,
            'utilization': used / self.container_volume if self.container_volume else 0.0,
            'evaluations': evals,
            'seconds': round(time.time() - t0, 1),
            'optimal_full': placed == total,
            'strategy': best_strategy or 'extreme_point_search',
            'beam_width': beam_info.get('beam_width', 0),
        }
        if verbose:
            print(f"  [增强版] 评估 {evals} 个方案，用时 {stats['seconds']}s，"
                  f"装入 {placed}/{total}，空间利用率 {stats['utilization']*100:.1f}%")
        if placed == total:
            return True, "成功", stats
        return False, (f"已尽最大努力搜索：装入 {placed}/{total} 件，"
                       f"空间利用率 {stats['utilization'] * 100:.1f}%（其余货物放不下）"), stats

    # --------------------------------------------------------------------- #
    #                              可视化                                    #
    # --------------------------------------------------------------------- #
    def create_cuboid(self, x, y, z, dx, dy, dz):
        v = np.array([[x, y, z], [x + dx, y, z], [x + dx, y + dy, z], [x, y + dy, z],
                      [x, y, z + dz], [x + dx, y, z + dz], [x + dx, y + dy, z + dz], [x, y + dy, z + dz]])
        return [[v[0], v[1], v[2], v[3]], [v[4], v[5], v[6], v[7]],
                [v[0], v[1], v[5], v[4]], [v[2], v[3], v[7], v[6]],
                [v[1], v[2], v[6], v[5]], [v[0], v[3], v[7], v[4]]]

    def update_plot(self, box_numbers=None):
        self.highlighted_boxes = set(box_numbers) if box_numbers else set()
        self.visualize()

    def visualize(self):
        from mpl_toolkits.mplot3d.art3d import Poly3DCollection
        from matplotlib.patches import Patch
        plt = _pyplot()
        plt.clf()
        fig = plt.figure(1, figsize=(13, 8))
        ax = fig.add_subplot(111, projection='3d')
        container_dims = [c / MM for c in self.container]
        ax.add_collection3d(Poly3DCollection(
            self.create_cuboid(0, 0, 0, *container_dims), alpha=0.1, facecolor='gray', edgecolor='black'))
        highlight_colors = plt.cm.tab10.colors
        legend_info = {}
        for (x, y, z, l, w, h), info in zip(self.packing_plan, self.box_colors):
            faces = self.create_cuboid(x / MM, y / MM, z / MM, l / MM, w / MM, h / MM)
            category = info['input_order'] + 1
            if category in self.highlighted_boxes:
                facecolor = highlight_colors[info['input_order'] % len(highlight_colors)]
                alpha = 0.8
                legend_info.setdefault(category, (facecolor, info))
            else:
                facecolor, alpha = 'lightgray', 0.3
            ax.add_collection3d(Poly3DCollection(faces, alpha=alpha, facecolor=facecolor, edgecolor='black'))
            ax.text(x / MM + l / (2 * MM), y / MM + w / (2 * MM), z / MM + h / (2 * MM),
                    str(info['number']), color='black', fontsize=10, ha='center', va='center')
        if legend_info:
            placed = Counter(info['input_order'] for info in self.box_colors)
            handles = []
            for category in sorted(legend_info):
                color, info = legend_info[category]
                dl, dw, dh = info['original_dimensions']
                # 图例数量为本方案实际装入的件数，未装满时附上应装总数
                packed, total = placed[info['input_order']], self.boxes[info['input_order']][3]
                qty = f"{packed}" if packed >= total else f"{packed}/{total}"
                handles.append(Patch(facecolor=color, edgecolor='black',
                                     label=f"Cat {category}: {dl:g} x {dw:g} x {dh:g} m  type {info['type']}  packed={qty}"))
            ax.legend(handles=handles, loc='upper left', bbox_to_anchor=(0.0, 1.0),
                      fontsize=9, framealpha=0.9, title='Highlighted categories (L x W x H)')
        ax.view_init(elev=20, azim=45)
        ax.set_xlabel('Length (m)')
        ax.set_ylabel('Width (m)')
        ax.set_zlabel('Height (m)')
        ax.set_xlim([0, container_dims[0]])
        ax.set_ylim([0, container_dims[1]])
        ax.set_zlim([0, container_dims[2]])
        ax.set_box_aspect(container_dims)
        title = 'Container Packing Visualization'
        if self.highlighted_boxes:
            title += f'\nHighlighted Categories: {sorted(self.highlighted_boxes)}'
        plt.title(title)
        plt.tight_layout()
        plt.draw()

    # --------------------------------------------------------------------- #
    #                              持久化                                    #
    # --------------------------------------------------------------------- #
    def save_packing_plan(self, file_path):
        data = {
            'container': [c / MM for c in self.container],
            'boxes': [[l / MM, w / MM, h / MM, n, weight, t] for (l, w, h, n, weight, t, i) in self.boxes],
            'packing_plan': [
                {'position': [p / MM for p in pos[:3]], 'dimensions': [p / MM for p in pos[3:]], 'box_info': info}
                for pos, info in zip(self.packing_plan, self.box_colors)],
        }
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    @staticmethod
    def load_packing_plan(file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)


# --------------------------------------------------------------------------- #
#                               输入 / 交互                                    #
# --------------------------------------------------------------------------- #
def list_files_in_directory(extensions):
    files = [f for f in os.listdir() if any(f.endswith(ext) for ext in extensions)]
    if not files:
        print(f"当前文件夹中没有 {extensions} 文件。")
    return files


def select_file(files):
    print("请选择文件（请根据下列内容选择数据源/模型）：")
    for i, file in enumerate(files):
        print(f"{i + 1}. {file}")
    try:
        idx = int(input("请输入文件编号: ")) - 1
    except ValueError:
        print("无效的文件编号！")
        return None
    if idx < 0 or idx >= len(files):
        print("无效的文件编号！")
        return None
    return files[idx]


def read_input_from_file(file_path):
    if file_path.endswith('.txt'):
        with open(file_path, 'r', encoding='utf-8') as file:
            # 跳过空行与整行注释（# 开头），兼容网页版导出的带注释清单
            lines = [line.strip() for line in file
                     if line.strip() and line.split('#')[0].strip()]
        container_parts = lines[0].split('#')[0].strip().split()
        container = tuple(to_mm(x) for x in container_parts[:3])
        box_count = int(lines[1].split('#')[0].strip())
        boxes = []
        for i in range(box_count):
            parts = lines[2 + i].split('#')[0].strip().split()
            if len(parts) < 6:
                raise ValueError(f"第 {3 + i} 行数据列数不足（需要 长 宽 高 数量 重量 类型），实际: {parts}")
            boxes.append((to_mm(parts[0]), to_mm(parts[1]), to_mm(parts[2]),
                          to_int(parts[3]), to_float(parts[4]), parts[5]))
        return container, boxes
    elif file_path.endswith('.xlsx'):
        import openpyxl
        wb = openpyxl.load_workbook(file_path, data_only=True)
        sheet = wb.active
        container = tuple(to_mm(cell.value) for cell in list(sheet[1])[:3])
        box_count = int(sheet.cell(row=2, column=1).value)
        boxes = []
        for i in range(3, 3 + box_count):
            row = [cell.value for cell in sheet[i]]
            boxes.append((to_mm(row[0]), to_mm(row[1]), to_mm(row[2]),
                          to_int(row[3]), to_float(row[4]), str(row[5])))
        return container, boxes
    raise ValueError("不支持的文件格式！")


def calculate_file_hash(file_path):
    hasher = hashlib.md5()
    with open(file_path, 'rb') as file:
        hasher.update(file.read())
    return hasher.hexdigest()


def print_plan(packer, sort_by_input_order=True):
    plan = list(zip(packer.packing_plan, packer.box_colors))
    if sort_by_input_order:
        plan.sort(key=lambda item: item[1]['input_order'])
    for (x, y, z, l, w, h), info in plan:
        print(f"箱子 {info['number']} (类型 {info['type']})")
        print(f"位置: ({format_decimal(x)}, {format_decimal(y)}, {format_decimal(z)})")
        print(f"尺寸: {format_decimal(l)} * {format_decimal(w)} * {format_decimal(h)}")
        print(f"重量: {format_weight(info['weight'])} kg\n")


def interactive_loop(packer):
    plt = _pyplot()
    packer.visualize()
    plt.ion()
    while True:
        try:
            s = input(f"\n输入箱子类别编号以高亮显示 (共 {len(packer.boxes)} 类，输入0退出): ")
            if s.strip() == "0":
                break
            nums = [int(x.strip()) for x in s.replace("，", ",").split(",") if x.strip()]
            if nums and all(0 < n <= len(packer.boxes) for n in nums):
                packer.update_plot(nums)
            else:
                print(f"包含无效的类别编号，请重试（有效范围：1 - {len(packer.boxes)}）。")
        except ValueError:
            print("请输入有效的数字编号。")
    plt.ioff()
    plt.show()


def choose_mode():
    """启动时选择 标准版 / 增强版。"""
    print("\n请选择装箱模式：")
    print("  1. 标准版   —— 与最初版结果一致，快速、稳定、可复现")
    print("  2. 增强版   —— 混合列模式、二维排样与有界束搜索，保证不劣于标准版（较慢）")
    choice = input("请输入选项 (1 或 2，直接回车默认 1): ").strip()
    if choice == "2":
        raw = input("增强版搜索时间上限(秒，直接回车默认 600): ").strip()
        try:
            budget = float(raw) if raw else 600.0
        except ValueError:
            budget = 600.0
        return "enhanced", budget
    return "standard", 0.0


def main():
    weight_threshold = float(input("请输入重量阈值（超过该重量的箱子不能叠放在其他箱子上，单位：kg）："))
    input_files = list_files_in_directory(['.txt', '.xlsx'])
    if not input_files:
        return
    input_file = select_file(input_files)
    if not input_file:
        return

    container_dims, boxes = read_input_from_file(input_file)
    # 命令行版：重量作为「底层优先」软指标（重货优先放底层，仍允许叠放）
    packer = ContainerPacker(container_dims, boxes, weight_threshold, bottom_metric='weight')

    mode, budget = choose_mode()
    if mode == "enhanced":
        print("\n增强版计算中，请稍候……")
        fits, result, _ = packer.pack_enhanced(time_budget=budget)
    else:
        fits, result = packer.pack()

    if fits:
        print("\n可以装入集装箱。具体装箱方案如下:")
        print_plan(packer, sort_by_input_order=True)
        packer.save_packing_plan("packing_plan.json")
        print("装箱方案已保存到 packing_plan.json")
        with open("data_hash.txt", "w") as hf:
            hf.write(calculate_file_hash(input_file))
        interactive_loop(packer)
    else:
        print(f"\n无法装入集装箱。原因: {result}")


def load_and_visualize():
    model_files = list_files_in_directory(['.json'])
    if not model_files:
        return
    model_file = select_file(model_files)
    if not model_file:
        return
    plan = ContainerPacker.load_packing_plan(model_file)
    container_dims = tuple(to_mm(x) for x in plan['container'])
    boxes = [(to_mm(l), to_mm(w), to_mm(h), int(n), float(weight), t)
             for l, w, h, n, weight, t in plan['boxes']]
    packer = ContainerPacker(container_dims, boxes)
    packer.packing_plan = [
        (to_mm(it['position'][0]), to_mm(it['position'][1]), to_mm(it['position'][2]),
         to_mm(it['dimensions'][0]), to_mm(it['dimensions'][1]), to_mm(it['dimensions'][2]))
        for it in plan['packing_plan']]
    packer.box_colors = [it['box_info'] for it in plan['packing_plan']]
    print("\n可以装入集装箱。具体装箱方案如下:")
    print_plan(packer, sort_by_input_order=False)
    interactive_loop(packer)


if __name__ == "__main__":
    if os.path.exists("packing_plan.json"):
        choice = input("检测到保存的装箱方案，请选择:\n1. 使用已有模型\n2. 重新运行并保存新模型\n请输入选项 (1 或 2): ")
        if choice == "1":
            load_and_visualize()
        elif choice == "2":
            print("重新计算装箱方案...")
            main()
        else:
            print("无效选项，程序退出。")
    else:
        print("未找到保存的装箱方案，开始计算装箱方案...")
        main()
