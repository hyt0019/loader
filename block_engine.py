# -*- coding: utf-8 -*-
"""块构建 + 极大空闲空间 + 束搜索（block-building beam search）。

思路来自单箱装载问题（single container loading problem）的主流方法：
Fanslau & Bortfeldt (2010) CLTRS、Zhu & Lim (2012) ID-GLA、Araya & Riff (2014) BSG。

- 块：同一类、同一朝向的货物先拼成 nx×ny×nz 的整齐长方体（简单块），成块摆放；
- 空间：柜内剩余空间用相互重叠的「极大空闲空间」表示，放块后按差集更新，不漏可放位置；
- 选择：每步取离柜角最近的空间，在该角放「体积大、留下无用缝隙少」的块；
- 搜索：束搜索每层保留若干部分方案，按贪心补全后的装载体积排序；束宽逐轮加倍，
  时间到即返回目前最好的方案（时间越长结果越好）。

所有坐标为整数毫米。支撑规则（与几何内核一致）：非落地货物的底面中心必须落在
下方接触面的凸包内部，即重心有支撑、不会悬空倾倒。
"""
import time

# 填充顺序：先沿哪个轴排满（0=长 x，1=宽 y，2=高 z），得到不同形状的简单块
_FILL_ORDERS = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))


# --------------------------------------------------------------------------- #
#                               支撑判断                                       #
# --------------------------------------------------------------------------- #
def _strictly_inside_hull(px, py, points):
    """点是否严格位于点集凸包内部（Andrew 单调链，整数精确计算）。"""
    pts = sorted(set(points))
    if len(pts) < 3:
        return False

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]  # 逆时针
    if len(hull) < 3:
        return False
    point = (px, py)
    for i in range(len(hull)):
        if cross(hull[i], hull[(i + 1) % len(hull)], point) <= 0:
            return False
    return True


def center_supported(x, y, l, w, rects):
    """底面中心是否被支撑：rects 为顶面恰好在本箱底面高度的矩形 (x1, y1, x2, y2)。

    中心点必须严格落在所有接触区域的凸包内部（横跨两箱的接缝也算有支撑）。
    坐标乘 2 后比较，避免奇数尺寸的半毫米中心。
    """
    cx, cy = 2 * x + l, 2 * y + w
    xe, ye = x + l, y + w
    corners = None
    for x1, y1, x2, y2 in rects:
        ix1 = x1 if x1 > x else x
        ix2 = x2 if x2 < xe else xe
        if ix2 <= ix1:
            continue
        iy1 = y1 if y1 > y else y
        iy2 = y2 if y2 < ye else ye
        if iy2 <= iy1:
            continue
        if 2 * ix1 < cx < 2 * ix2 and 2 * iy1 < cy < 2 * iy2:
            return True
        if corners is None:
            corners = []
        corners += ((2 * ix1, 2 * iy1), (2 * ix2, 2 * iy1), (2 * ix2, 2 * iy2), (2 * ix1, 2 * iy2))
    if corners is None or len(corners) < 8:
        return False
    return _strictly_inside_hull(cx, cy, corners)


# --------------------------------------------------------------------------- #
#                              块构建搜索                                      #
# --------------------------------------------------------------------------- #
class _State:
    """部分装载方案。chain 为放置记录的链表 (placement, parent_chain)，复制开销为常数。"""
    __slots__ = ('spaces', 'remaining', 'tops', 'volume', 'count', 'chain')

    def __init__(self, spaces, remaining, tops, volume, count, chain):
        self.spaces = spaces
        self.remaining = remaining
        self.tops = tops
        self.volume = volume
        self.count = count
        self.chain = chain

    def placements(self):
        out = []
        node = self.chain
        while node is not None:
            out.append(node[0])
            node = node[1]
        out.reverse()
        return out


class BlockSearch:
    """kinds: [(允许朝向列表 [(a, b, c), ...], 件数), ...]；朝向第三维为高度。"""

    def __init__(self, container, kinds):
        self.L, self.W, self.H = (int(v) for v in container)
        self.orients = [tuple(sorted(set(o))) for o, _ in kinds]
        self.counts = tuple(int(n) for _, n in kinds)
        self.volumes = [o[0][0] * o[0][1] * o[0][2] if o else 0 for o in self.orients]
        self.kind_mins = [
            (min(a for a, _, _ in o), min(b for _, b, _ in o), min(c for _, _, c in o)) if o else None
            for o in self.orients]
        self.total_volume = sum(v * n for v, n in zip(self.volumes, self.counts))
        self.evaluations = 0

    # ---------------- 基本操作 ---------------- #
    def root(self):
        spaces = [(0, 0, 0, self.L, self.W, self.H)] if min(self.L, self.W, self.H) > 0 else []
        return _State(spaces, list(self.counts), {}, 0, 0, None)

    def _mins(self, remaining):
        """剩余货物在三个方向上可能的最小尺寸；货物已全部装完时返回 None。"""
        mx = my = mz = None
        for k, r in enumerate(remaining):
            if r and self.kind_mins[k] is not None:
                a, b, c = self.kind_mins[k]
                if mx is None:
                    mx, my, mz = a, b, c
                else:
                    if a < mx:
                        mx = a
                    if b < my:
                        my = b
                    if c < mz:
                        mz = c
        return None if mx is None else (mx, my, mz)

    def _pick_space(self, spaces):
        """离柜角最近的空间（只看底面四角，三向距离从小到大比较），同距离取体积大者。"""
        L, W = self.L, self.W
        best_i, best_key = -1, None
        for i, (x1, y1, z1, x2, y2, z2) in enumerate(spaces):
            dx = x1 if x1 <= L - x2 else L - x2
            dy = y1 if y1 <= W - y2 else W - y2
            if dx <= dy:
                a, b = dx, dy
            else:
                a, b = dy, dx
            if z1 < a:
                key = (z1, a, b)
            elif z1 < b:
                key = (a, z1, b)
            else:
                key = (a, b, z1)
            key = key + (-(x2 - x1) * (y2 - y1) * (z2 - z1),)
            if best_key is None or key < best_key:
                best_i, best_key = i, key
        return best_i

    def _candidates(self, space, remaining, mins):
        """为空间生成候选简单块，按评分从高到低排序。

        评分 = 块体积 - 估计浪费：块与空间之间若剩下的缝隙比任何剩余货物都窄，
        这条缝就再也用不上，按缝隙体积扣分。
        """
        x1, y1, z1, x2, y2, z2 = space
        sx, sy, sz = x2 - x1, y2 - y1, z2 - z1
        mnx, mny, mnz = mins
        out = []
        for k, r in enumerate(remaining):
            if not r:
                continue
            unit = self.volumes[k]
            for a, b, c in self.orients[k]:
                if a > sx or b > sy or c > sz:
                    continue
                caps = (sx // a, sy // b, sz // c)
                if caps[0] * caps[1] * caps[2] <= r:
                    shapes = (caps,)
                else:
                    shapes = set()
                    for first, second, third in _FILL_ORDERS:
                        n = [0, 0, 0]
                        n[first] = min(caps[first], r)
                        n[second] = min(caps[second], r // n[first])
                        n[third] = min(caps[third], r // (n[first] * n[second]))
                        shapes.add((n[0], n[1], n[2]))
                for nx, ny, nz in shapes:
                    bx, by, bz = nx * a, ny * b, nz * c
                    volume = nx * ny * nz * unit
                    waste = 0
                    rx, ry, rz = sx - bx, sy - by, sz - bz
                    if 0 < rx < mnx:
                        waste += rx * by * bz
                    if 0 < ry < mny:
                        waste += bx * ry * bz
                    if 0 < rz < mnz:
                        waste += bx * by * rz
                    out.append((volume - waste, volume, k, a, b, c, nx, ny, nz))
        out.sort(reverse=True)
        return out

    def _position(self, space, cand, tops):
        """在空间底面四角中依次尝试（离柜角近的优先），返回第一个底层全部有支撑的位置。"""
        x1, y1, z1, x2, y2, z2 = space
        _, _, _, a, b, _, nx, ny, _ = cand
        bx, by = nx * a, ny * b
        left = x1 <= self.L - x2
        front = y1 <= self.W - y2
        xs = (x1, x2 - bx) if left else (x2 - bx, x1)
        ys = (y1, y2 - by) if front else (y2 - by, y1)
        if z1 == 0:
            return xs[0], ys[0]
        rects = tops.get(z1)
        if not rects:
            return None
        tried = set()
        for px in xs:
            for py in ys:
                if (px, py) in tried:
                    continue
                tried.add((px, py))
                if self._block_supported(px, py, a, b, nx, ny, rects):
                    return px, py
        return None

    @staticmethod
    def _block_supported(px, py, a, b, nx, ny, rects):
        qx, qy = px + nx * a, py + ny * b
        for x1, y1, x2, y2 in rects:  # 整块底面落在同一个支撑面内：每件都有支撑
            if x1 <= px and y1 <= py and qx <= x2 and qy <= y2:
                return True
        for i in range(nx):
            for j in range(ny):
                if not center_supported(px + i * a, py + j * b, a, b, rects):
                    return False
        return True

    @staticmethod
    def _update_spaces(spaces, box, mins):
        """放入长方体 box 后更新极大空闲空间：与之相交的空间切成至多 6 块，去掉被包含和过小的。"""
        bx1, by1, bz1, bx2, by2, bz2 = box
        mnx, mny, mnz = mins
        kept, fresh = [], []
        for s in spaces:
            x1, y1, z1, x2, y2, z2 = s
            if bx1 >= x2 or bx2 <= x1 or by1 >= y2 or by2 <= y1 or bz1 >= z2 or bz2 <= z1:
                kept.append(s)
                continue
            sy_ok, sz_ok, sx_ok = y2 - y1 >= mny, z2 - z1 >= mnz, x2 - x1 >= mnx
            if bx1 - x1 >= mnx and sy_ok and sz_ok:
                fresh.append((x1, y1, z1, bx1, y2, z2))
            if x2 - bx2 >= mnx and sy_ok and sz_ok:
                fresh.append((bx2, y1, z1, x2, y2, z2))
            if by1 - y1 >= mny and sx_ok and sz_ok:
                fresh.append((x1, y1, z1, x2, by1, z2))
            if y2 - by2 >= mny and sx_ok and sz_ok:
                fresh.append((x1, by2, z1, x2, y2, z2))
            if bz1 - z1 >= mnz and sx_ok and sy_ok:
                fresh.append((x1, y1, z1, x2, y2, bz1))
            if z2 - bz2 >= mnz and sx_ok and sy_ok:
                fresh.append((x1, y1, bz2, x2, y2, z2))
        # 原有空间彼此不包含，新空间也不可能包含原有空间；只需剔除被包含的新空间
        result = kept
        fresh = sorted(set(fresh), key=lambda s: (s[3] - s[0]) * (s[4] - s[1]) * (s[5] - s[2]),
                       reverse=True)
        accepted = []
        for f in fresh:
            f0, f1, f2, f3, f4, f5 = f
            contained = False
            for t in accepted:
                if t[0] <= f0 and t[1] <= f1 and t[2] <= f2 and f3 <= t[3] and f4 <= t[4] and f5 <= t[5]:
                    contained = True
                    break
            if not contained:
                for t in kept:
                    if t[0] <= f0 and t[1] <= f1 and t[2] <= f2 and f3 <= t[3] and f4 <= t[4] and f5 <= t[5]:
                        contained = True
                        break
            if not contained:
                accepted.append(f)
        result.extend(accepted)
        return result

    def _apply(self, spaces, remaining, tops, space, cand, pos, mins):
        """把候选块放到 pos，原地更新 remaining / tops，返回新的空间列表与放置记录。"""
        _, volume, k, a, b, c, nx, ny, nz = cand
        px, py = pos
        z = space[2]
        box = (px, py, z, px + nx * a, py + ny * b, z + nz * c)
        remaining[k] -= nx * ny * nz
        top = box[5]
        tops[top] = tops.get(top, ()) + ((box[0], box[1], box[3], box[4]),)
        new_mins = self._mins(remaining) or mins
        return self._update_spaces(spaces, box, new_mins), (px, py, z, a, b, c, nx, ny, nz, k), volume

    # ---------------- 贪心补全 ---------------- #
    def complete(self, state, deadline=None):
        """从部分方案出发，每步取最高分的可行块，直到放不下为止。"""
        spaces = list(state.spaces)
        remaining = list(state.remaining)
        tops = dict(state.tops)
        volume, count, chain = state.volume, state.count, state.chain
        while spaces:
            mins = self._mins(remaining)
            if mins is None or (deadline is not None and time.time() > deadline):
                break
            placed = False
            while spaces:
                i = self._pick_space(spaces)
                space = spaces[i]
                for cand in self._candidates(space, remaining, mins):
                    pos = self._position(space, cand, tops)
                    if pos is not None:
                        spaces, record, gained = self._apply(spaces, remaining, tops, space, cand, pos, mins)
                        volume += gained
                        count += cand[6] * cand[7] * cand[8]
                        chain = (record, chain)
                        placed = True
                        break
                if placed:
                    break
                spaces.pop(i)  # 该空间放不下任何剩余货物
        self.evaluations += 1
        return _State(spaces, remaining, tops, volume, count, chain)

    def _children(self, state, width):
        """在当前选中的空间里展开前 width 个可行块，得到子方案。"""
        spaces = list(state.spaces)
        mins = self._mins(state.remaining)
        if mins is None:
            return []
        while spaces:
            i = self._pick_space(spaces)
            space = spaces[i]
            children = []
            for cand in self._candidates(space, state.remaining, mins):
                pos = self._position(space, cand, state.tops)
                if pos is None:
                    continue
                remaining = list(state.remaining)
                tops = dict(state.tops)
                new_spaces, record, gained = self._apply(spaces, remaining, tops, space, cand, pos, mins)
                children.append(_State(new_spaces, remaining, tops, state.volume + gained,
                                       state.count + cand[6] * cand[7] * cand[8], (record, state.chain)))
                if len(children) >= width:
                    break
            if children:
                return children
            spaces.pop(i)
        return []

    # ---------------- 对外 ---------------- #
    def greedy(self):
        return self.complete(self.root())

    def beam_search(self, deadline, best=None):
        """束搜索：每层每个方案展开若干块，子方案以贪心补全后的体积排序保留前 w 个。

        束宽 w 逐轮加倍，直到 deadline；全部装入时提前结束。若某轮束宽并未截断任何
        子方案，说明加宽无效，改为增加每个方案展开的块数；两者都未起限制作用时，
        搜索已经穷尽，直接结束。
        返回 (最好的完整方案, 已完成的最大束宽)。
        """
        if best is None:
            best = self.greedy()
        width, branch, finished = 1, 2, 0
        while time.time() < deadline and best.volume < self.total_volume:
            beam = [self.root()]
            truncated = branched = False
            while beam and time.time() < deadline:
                scored = []
                for node in beam:
                    children = self._children(node, branch)
                    if len(children) >= branch:
                        branched = True
                    for child in children:
                        final = self.complete(child, deadline)
                        if final.volume > best.volume or (
                                final.volume == best.volume and final.count > best.count):
                            best = final
                        scored.append((final.volume, final.count, child))
                        if time.time() >= deadline or best.volume >= self.total_volume:
                            return best, finished
                scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
                beam, seen = [], set()
                for _, _, child in scored:
                    signature = (tuple(child.remaining), child.volume, len(child.spaces))
                    if signature in seen:
                        continue
                    if len(beam) >= width:
                        truncated = True
                        break
                    seen.add(signature)
                    beam.append(child)
            if time.time() >= deadline:
                break
            finished = width
            if truncated:
                width *= 2
                branch = max(branch, min(width, 6))
            elif branched:
                branch *= 2
            else:
                break
        return best, finished
