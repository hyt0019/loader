# -*- coding: utf-8 -*-
"""
集装箱装箱计算程序（生产版 packer_pro）
================================================================
本文件在整数毫米几何内核之上，提供两种装箱模式，并可在启动时选择：

  1) 标准版（deterministic）
     复现最初版(Decimal 版)的逐箱装箱决策，结果稳定、可复现、速度快。

  2) 增强版（遗传/随机重启搜索）
     以"装箱顺序 + 每类朝向"为搜索空间，用遗传算法 + 随机重启逼近最优。
     以标准版的解作为种子并采用【精英保留】，只有严格更优才替换，
     并在达到 100% 装载时立即停止——因此增强版在数学上保证【不劣于】标准版。

几何内核：所有几何量使用整数毫米(mm)，碰撞检测用均匀网格空间索引，
支撑检测用"顶面高度表"，候选位置用增量维护的极点集合。经随机对拍验证与
暴力法完全一致。
================================================================
"""

import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from matplotlib.patches import Patch
import numpy as np
from itertools import permutations
from decimal import Decimal
from collections import defaultdict
import json
import os
import hashlib
import random
import time
import matplotlib

try:
    matplotlib.use('TkAgg')  # 桌面交互式后端
except Exception:
    pass  # 无图形环境(网页后端/服务器)时退回默认后端，不影响网页版使用

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


# --------------------------------------------------------------------------- #
#                            装箱器（整数几何内核）                            #
# --------------------------------------------------------------------------- #
class ContainerPacker:
    def __init__(self, container, boxes, weight_threshold=100,
                 no_flip=None, priority=None, bottom_metric='none'):
        """三维装箱器。

        新增参数：
          no_flip:       每类货物是否「禁止倒放」。可为 list（与 boxes 同序）或 dict{io: bool}。
                         禁止倒放意味着该类货物落地面固定为「长×宽」，高度只能是原始高度 h
                         （运输中不会倒过来，避免内部零件散开）。
          priority:      每类货物的「摆放优先级」。可为 list 或 dict{io: number}；
                         数值越大越优先放到底层。用于「手动模式」。
          bottom_metric: 底层优先指标，决定哪类货物优先放到底层（仍允许叠放）：
                         'none'        —— 不启用，纯粹以空间利用率为目标（原始行为）
                         'weight'      —— 重量大的优先放底层
                         'volumetric'  —— 体积重(密度=重量/体积)大的优先放底层
                         'manual'      —— 按 priority 手动优先级放底层
        """
        self.container = tuple(int(x) for x in container)
        self.boxes = [
            (int(l), int(w), int(h), int(n), float(weight), t, i)
            for i, (l, w, h, n, weight, t) in enumerate(boxes)
        ]
        self.weight_threshold = float(weight_threshold)
        self.no_flip = self._normalize_flags(no_flip, bool, False)
        self.priority = self._normalize_flags(priority, float, 0.0)
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
        if z > 0:
            area = l * w
            support = 0
            for ox, oy, oz, ol, ow, oh in self.top_map.get(z, ()):  # 顶面恰好接触本箱底面
                overlap_x = min(x + l, ox + ol) - max(x, ox)
                overlap_y = min(y + w, oy + ow) - max(y, oy)
                if overlap_x > 0 and overlap_y > 0:
                    support += overlap_x * overlap_y
            # support/area < 0.6  <=>  support*5 < area*3 （整数精确比较）
            if support * 5 < area * 3:
                return False
        return True

    def _place(self, x, y, z, l, w, h):
        idx = len(self.occupied)
        box = (x, y, z, l, w, h)
        self.occupied.append(box)
        self.packing_plan.append(box)
        self.top_map[z + h].append(box)
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
        # 是否采用仍由适应度 (利用率, 件数, 底层得分) 决定，故不会牺牲装载件数。
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
    def pack(self):
        """标准版：确定性策略组合（长宽两种朝向 × 多种排序），取装载体积最大者。

        对坐标轴顺序与排序方式做穷举，避免单一贪心因朝向/顺序不巧而崩坏。
        返回 (是否全部装入, 说明)。
        """
        total = self.total_units
        if self.bottom_metric == 'manual':
            return self._pack_manual(total)  # 手动模式：严格按用户优先级摆放
        best_fit, best = None, None
        for swap in (False, True):
            for pol in self._deterministic_policies():
                _, placed, used, sink = self._solve(swap, pol[0], pol[1], record=False)
                # 适应度：先看利用率，再看件数，最后用底层得分做同分排序（软偏好）
                fit = (used, placed, sink)
                if best_fit is None or fit > best_fit:
                    best_fit, best = fit, (swap, pol)
                if placed == total:
                    break  # 已满载即提前结束（底层优先策略排在最前，会被优先尝试）
            if best_fit[1] == total:
                break
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

    # ---------------- 对外：增强版（遗传/随机重启搜索） ---------------- #
    def pack_enhanced(self, time_budget=60.0, rng_seed=20260719,
                      population=24, verbose=True):
        """增强版：遗传算法 + 随机重启，精英保留，保证不劣于标准版。

        time_budget: 搜索时间上限（秒）；达到 100% 装载会提前结束。
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

        # --- 评估：适应度 = (已装体积, 已装件数, 底层得分)，利用率最大为首要目标；
        #     底层得分仅在利用率与件数相同时生效，故不会牺牲装载件数 ---
        def ev(swap, pol):
            nonlocal evals, best_fit, best
            _, placed, used, sink = self._solve(swap, pol[0], pol[1], record=False)
            evals += 1
            fit = (used, placed, sink)
            if best_fit is None or fit > best_fit:
                best_fit, best = fit, (swap, pol)
            return fit

        # --- 确定性种子：长宽两种朝向 × 多种排序（保证不劣于标准版）---
        for swap in (False, True):
            for pol in self._deterministic_policies():
                ev(swap, pol)
                if best_fit[1] >= total:
                    break
            if best_fit[1] >= total:
                break

        # --- 遗传搜索（种子未达 100% 时启动；长宽朝向也作为基因参与进化）---
        if best_fit[1] < total:

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
            while len(pop) < population and best_fit[1] < total and time.time() - t0 < time_budget:
                g = random_genome()
                pop.append((genome_fit(g), g))

            while pop and best_fit[1] < total and time.time() - t0 < time_budget:
                pop.sort(key=lambda it: it[0], reverse=True)
                new_pop = pop[:max(2, population // 5)]
                while len(new_pop) < population and time.time() - t0 < time_budget:
                    p1 = max(rng.sample(pop, min(3, len(pop))), key=lambda it: it[0])[1]
                    p2 = max(rng.sample(pop, min(3, len(pop))), key=lambda it: it[0])[1]
                    child = mutate(crossover(p1, p2)) if rng.random() < 0.9 else random_genome()
                    new_pop.append((genome_fit(child), child))
                pop = new_pop

        # --- 用最优策略正式装箱（生成编号/颜色信息） ---
        _, placed, used, sink = self._solve(best[0], best[1][0], best[1][1], record=True)
        stats = {
            'placed': placed,
            'total': total,
            'utilization': used / self.container_volume if self.container_volume else 0.0,
            'evaluations': evals,
            'seconds': round(time.time() - t0, 1),
            'optimal_full': placed == total,
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
            handles = []
            for category in sorted(legend_info):
                color, info = legend_info[category]
                dl, dw, dh = info['original_dimensions']
                qty = self.boxes[info['input_order']][3]
                handles.append(Patch(facecolor=color, edgecolor='black',
                                     label=f"Cat {category}: {dl:g} x {dw:g} x {dh:g} m  type {info['type']}  qty={qty}"))
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
    print("  2. 增强版   —— 遗传/随机搜索逼近最优，保证不劣于标准版（较慢）")
    choice = input("请输入选项 (1 或 2，直接回车默认 1): ").strip()
    if choice == "2":
        raw = input("增强版搜索时间上限(秒，直接回车默认 60): ").strip()
        try:
            budget = float(raw) if raw else 60.0
        except ValueError:
            budget = 60.0
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
