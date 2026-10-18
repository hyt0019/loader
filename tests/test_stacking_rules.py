"""叠放规则：严格执行底层优先指标（重 / 密度大的不压在轻 / 密度小的上面）与「不可压」。

运行：python -m unittest discover -s tests
"""
import io
import json
import random
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from block_engine import FREE_STACK, can_stack, center_supported  # noqa: E402
from packer_pro import ContainerPacker  # noqa: E402
from planner_core import (build_cargo_xlsx, build_excel_bytes, build_json_bytes,  # noqa: E402
                          load_saved_plan, parse_cargo_xlsx, parse_xlsx, saved_plan_form_data)

# 客户 HT2619C 清单（20 尺柜）：活塞密度约 110 kg/m³，内漏测试台约 59 kg/m³。
# 不加规则时活塞会整件压在内漏测试台上。
HT2619C = [
    (5530, 760, 1970, 1, 600.0, '0'),   # 水表台-测试台
    (1930, 1200, 2230, 1, 700.0, '0'),  # 水表台-水箱
    (680, 680, 1830, 1, 300.0, '0'),    # 水表台-稳压罐
    (2300, 700, 850, 1, 150.0, '0'),    # 水表台-活塞
    (850, 880, 1200, 1, 200.0, '0'),    # 水表台-冷水机
    (900, 650, 1150, 1, 130.0, '0'),    # 拧表机
    (1660, 1300, 1330, 1, 170.0, '0'),  # 内漏测试台
    (1100, 560, 1150, 2, 30.0, '0'),    # 水表中转车
]
HT2619C_NO_FLIP = [True] * 7 + [False]
CONTAINER_20GP = (5800, 2350, 2350)
PISTON, LEAK_TESTER = 3, 6


def contacts(packer):
    """上下直接接触的货物对 (下层序号, 上层序号)：上层底面 == 下层顶面且投影有正面积重叠。"""
    plan = packer.packing_plan
    pairs = []
    for i, (x, y, z, l, w, h) in enumerate(plan):
        for j, (x2, y2, z2, l2, w2, h2) in enumerate(plan):
            if i != j and z2 == z + h and x < x2 + l2 and x2 < x + l and y < y2 + w2 and y2 < y + w:
                pairs.append((i, j))
    return pairs


def independent_attr(packer, io):
    """不依赖内核实现，按定义重新计算叠放属性。"""
    l, w, h, n, weight = packer.boxes[io][:5]
    value = None
    if packer.strict_stack and weight > 0:
        value = weight if packer.bottom_metric == 'weight' else weight * 1e9 / (l * w * h)
    return value, bool(packer.no_stack.get(io))


def assert_rules_hold(case, packer):
    """独立复核：不出界、不重叠、重心有支撑，且每一对上下接触的货物都符合叠放规则。"""
    length, width, height = packer.container
    plan = packer.packing_plan
    ios = [info['input_order'] for info in packer.box_colors]
    for index, (x, y, z, l, w, h) in enumerate(plan):
        case.assertTrue(0 <= x and 0 <= y and 0 <= z)
        case.assertTrue(x + l <= length and y + w <= width and z + h <= height)
        if z > 0:
            tops = [(ox, oy, ox + ol, oy + ow) for ox, oy, oz, ol, ow, oh in plan if oz + oh == z]
            case.assertTrue(center_supported(x, y, l, w, tops), f'第 {index + 1} 件货物重心悬空')
        for ox, oy, oz, ol, ow, oh in plan[index + 1:]:
            case.assertFalse(x < ox + ol and x + l > ox and y < oy + ow and y + w > oy
                             and z < oz + oh and z + h > oz)
    for lower, upper in contacts(packer):
        low, up = independent_attr(packer, ios[lower]), independent_attr(packer, ios[upper])
        case.assertFalse(low[1], f'第 {upper + 1} 件压在不可压的第 {lower + 1} 件上')
        if low[0] is not None and up[0] is not None:
            case.assertLessEqual(up[0], low[0] * (1 + 1e-9),
                                 f'第 {upper + 1} 件比它压着的第 {lower + 1} 件更重 / 密度更大')


def random_instance(rng):
    container = rng.choice([(5800, 2350, 2350), (4000, 2200, 2000)])
    boxes = []
    for _ in range(rng.randint(3, 8)):
        dims = [rng.randint(300, 1500) for _ in range(3)]
        boxes.append((dims[0], dims[1], dims[2], rng.randint(1, 25),
                      float(rng.choice([0, rng.randint(5, 900)])), '0'))
    no_flip = [rng.random() < 0.3 for _ in boxes]
    no_stack = [rng.random() < 0.25 for _ in boxes]
    return container, boxes, no_flip, no_stack


class CanStackTests(unittest.TestCase):
    def test_rules(self):
        self.assertTrue(can_stack((50.0, False), (60.0, False)))
        self.assertTrue(can_stack((60.0, False), (60.0, False)))  # 同一种货可以互相叠放
        self.assertFalse(can_stack((61.0, False), (60.0, False)))
        self.assertTrue(can_stack((None, False), (10.0, False)))   # 未填重量不参与比较
        self.assertTrue(can_stack((99.0, False), (None, False)))
        self.assertFalse(can_stack(FREE_STACK, (None, True)))      # 不可压：顶上什么都不能放
        self.assertTrue(can_stack((None, True), FREE_STACK))       # 不可压的货本身可以压在别的货上


class KernelTests(unittest.TestCase):
    def packer(self, boxes, **kwargs):
        packer = ContainerPacker((3000, 1000, 2000), boxes, **kwargs)
        packer._reset_state()
        return packer

    def test_strict_density_blocks_dense_on_light(self):
        # 0：大而轻（密度 20），1：小而重（密度 400）
        boxes = [(1000, 1000, 1000, 1, 20.0, '0'), (500, 500, 500, 1, 50.0, '0')]
        packer = self.packer(boxes, bottom_metric='volumetric', strict_stack=True)
        packer._place(0, 0, 0, 1000, 1000, 1000, 0)
        self.assertFalse(packer.can_place(250, 250, 1000, 500, 500, 500, 50.0, 1))
        # 不严格执行时仍允许（原来的软偏好行为）
        soft = self.packer(boxes, bottom_metric='volumetric')
        soft._place(0, 0, 0, 1000, 1000, 1000, 0)
        self.assertTrue(soft.can_place(250, 250, 1000, 500, 500, 500, 50.0, 1))

    def test_strict_by_weight_compares_weight_not_density(self):
        boxes = [(1000, 1000, 1000, 1, 100.0, '0'), (500, 500, 500, 1, 90.0, '0')]
        packer = self.packer(boxes, bottom_metric='weight', strict_stack=True)
        packer._place(0, 0, 0, 1000, 1000, 1000, 0)
        self.assertTrue(packer.can_place(250, 250, 1000, 500, 500, 500, 90.0, 1))

    def test_strict_needs_a_metric(self):
        packer = ContainerPacker((1000, 1000, 1000), [(100, 100, 100, 1, 1.0, '0')], strict_stack=True)
        self.assertFalse(packer.strict_stack)
        self.assertFalse(packer.stack_rules)

    def test_missing_weight_is_not_compared(self):
        boxes = [(1000, 1000, 1000, 1, 0.0, '0'), (500, 500, 500, 1, 50.0, '0')]
        packer = self.packer(boxes, bottom_metric='volumetric', strict_stack=True)
        packer._place(0, 0, 0, 1000, 1000, 1000, 0)
        self.assertTrue(packer.can_place(250, 250, 1000, 500, 500, 500, 50.0, 1))

    def test_nothing_on_no_stack_box(self):
        boxes = [(1000, 1000, 1000, 1, 10.0, '0'), (500, 500, 500, 1, 1.0, '0')]
        packer = self.packer(boxes, no_stack=[True, False])
        packer._place(0, 0, 0, 1000, 1000, 1000, 0)
        self.assertFalse(packer.can_place(250, 250, 1000, 500, 500, 500, 1.0, 1))
        self.assertTrue(packer.can_place(1000, 0, 0, 500, 500, 500, 1.0, 1))

    def test_box_under_flush_overhang_counts_as_supporting_it(self):
        # 0 落地；1 压在 0 上并向右悬挑 500；2 若恰好塞到悬挑下方且顶面齐平，就会被 1 压着
        boxes = [(1000, 1000, 500, 1, 10.0, '0'), (1500, 1000, 300, 1, 10.0, '0'),
                 (500, 1000, 500, 1, 10.0, '0')]
        packer = self.packer(boxes, no_stack=[False, False, True])
        packer._place(0, 0, 0, 1000, 1000, 500, 0)
        self.assertTrue(packer.can_place(0, 0, 500, 1500, 1000, 300, 10.0, 1))
        packer._place(0, 0, 500, 1500, 1000, 300, 1)
        self.assertFalse(packer.can_place(1000, 0, 0, 500, 1000, 500, 10.0, 2))
        free = self.packer(boxes)
        free._place(0, 0, 0, 1000, 1000, 500, 0)
        free._place(0, 0, 500, 1500, 1000, 300, 1)
        self.assertTrue(free.can_place(1000, 0, 0, 500, 1000, 500, 10.0, 2))


class CustomerCaseTests(unittest.TestCase):
    def run_case(self, mode, **kwargs):
        packer = ContainerPacker(CONTAINER_20GP, HT2619C, no_flip=HT2619C_NO_FLIP,
                                 bottom_metric='volumetric', **kwargs)
        if mode == 'standard':
            fits, message = packer.pack()
        else:
            fits, message, _ = packer.pack_enhanced(time_budget=5, verbose=False)
        return packer, fits, message

    def placed_on(self, packer, io):
        """io 类货物直接压着的货物类别。"""
        ios = [info['input_order'] for info in packer.box_colors]
        return {ios[lower] for lower, upper in contacts(packer) if ios[upper] == io}

    def test_strict_density_keeps_piston_off_leak_tester_and_fits_all(self):
        for mode in ('standard', 'enhanced'):
            with self.subTest(mode=mode):
                packer, fits, message = self.run_case(mode, strict_stack=True)
                self.assertTrue(fits, message)
                self.assertNotIn(LEAK_TESTER, self.placed_on(packer, PISTON))
                assert_rules_hold(self, packer)

    def test_no_stack_leak_tester_carries_nothing(self):
        no_stack = [io == LEAK_TESTER for io in range(len(HT2619C))]
        for mode in ('standard', 'enhanced'):
            for strict in (False, True):
                with self.subTest(mode=mode, strict=strict):
                    packer, fits, message = self.run_case(mode, no_stack=no_stack, strict_stack=strict)
                    self.assertTrue(fits, message)
                    ios = [info['input_order'] for info in packer.box_colors]
                    self.assertFalse(any(ios[lower] == LEAK_TESTER for lower, _ in contacts(packer)))
                    assert_rules_hold(self, packer)


class SearchPathTests(unittest.TestCase):
    """各条搜索路径（块构建 / 组合块 / 列模式 / 逐箱策略）在规则下都只产生合规方案。"""

    def test_rules_off_gives_identical_plan(self):
        rng = random.Random(7)
        for _ in range(5):
            container, boxes, no_flip, _ = random_instance(rng)
            base = ContainerPacker(container, boxes, no_flip=no_flip, bottom_metric='volumetric')
            base.pack()
            same = ContainerPacker(container, boxes, no_flip=no_flip, bottom_metric='volumetric',
                                   no_stack=[False] * len(boxes), strict_stack=False)
            same.pack()
            self.assertEqual(base.packing_plan, same.packing_plan)

    def test_standard_and_enhanced_respect_rules_on_random_lists(self):
        rng = random.Random(20261018)
        for index in range(8):
            container, boxes, no_flip, no_stack = random_instance(rng)
            metric = ('weight', 'volumetric')[index % 2]
            for mode in ('standard', 'enhanced'):
                with self.subTest(index=index, mode=mode):
                    packer = ContainerPacker(container, boxes, no_flip=no_flip, bottom_metric=metric,
                                             no_stack=no_stack, strict_stack=True)
                    if mode == 'standard':
                        packer.pack()
                    else:
                        packer.pack_enhanced(time_budget=1.5, verbose=False)
                    assert_rules_hold(self, packer)

    def test_column_strategies_respect_rules(self):
        boxes = [(400, 400, 400, 120, 40.0, 'A'), (400, 400, 300, 60, 8.0, 'B'),
                 (400, 400, 500, 30, 2.0, 'C')]
        for no_stack in ([False, False, False], [False, True, False], [True, True, True]):
            with self.subTest(no_stack=no_stack):
                packer = ContainerPacker((4000, 2000, 2000), boxes, no_flip=[True] * 3,
                                         bottom_metric='volumetric', strict_stack=True,
                                         no_stack=no_stack)
                candidates = [packer._column_beam_candidate(deadline=time.time() + 5),
                              packer._layer_column_candidate(deadline=time.time() + 5),
                              packer._dense_column_candidate(deadline=time.time() + 5),
                              packer._block_candidate(general=True)[0]]
                self.assertTrue(any(candidates))
                for candidate in filter(None, candidates):
                    packer._restore_plan(candidate[3], candidate[4])
                    assert_rules_hold(self, packer)

    def test_all_no_stack_means_single_layer(self):
        boxes = [(500, 500, 400, 30, 10.0, '0'), (600, 400, 300, 20, 5.0, '1')]
        for mode in ('standard', 'enhanced'):
            packer = ContainerPacker((2000, 2000, 2000), boxes, no_stack=[True, True])
            if mode == 'standard':
                packer.pack()
            else:
                packer.pack_enhanced(time_budget=1, verbose=False)
            self.assertTrue(packer.packing_plan)
            self.assertTrue(all(z == 0 for _, _, z, _, _, _ in packer.packing_plan))


class FileRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.packer = ContainerPacker(
            CONTAINER_20GP, HT2619C, no_flip=HT2619C_NO_FLIP, bottom_metric='volumetric',
            strict_stack=True, no_stack=[io == LEAK_TESTER for io in range(len(HT2619C))],
            descriptions=[f'货物{io + 1}' for io in range(len(HT2619C))])
        self.packer.pack()
        self.stats = {'placed': len(self.packer.packing_plan), 'total': self.packer.total_units,
                      'utilization': 0.5, 'evaluations': 1, 'seconds': 0.0}

    def assert_rules_restored(self, imported):
        self.assertTrue(imported.strict_stack)
        self.assertEqual(imported.bottom_metric, 'volumetric')
        self.assertEqual([imported.no_stack.get(io, False) for io in range(len(HT2619C))],
                         [io == LEAK_TESTER for io in range(len(HT2619C))])
        form = saved_plan_form_data(imported)
        self.assertEqual(form['no_stack'], [io == LEAK_TESTER for io in range(len(HT2619C))])
        self.assertEqual(form['strict_stack'], 'volumetric')

    def test_json_round_trip(self):
        content = build_json_bytes(self.packer, 'HT2619C')
        data = json.loads(content)
        self.assertEqual(data['strict_stack'], 'volumetric')
        self.assertTrue(data['boxes'][LEAK_TESTER][9])
        imported, ship_no = load_saved_plan('plan.json', content)
        self.assertEqual(ship_no, 'HT2619C')
        self.assert_rules_restored(imported)

    def test_plan_xlsx_round_trip(self):
        content = build_excel_bytes(self.packer, self.stats, 'HT2619C')
        imported, _ = load_saved_plan('plan.xlsx', content)
        self.assert_rules_restored(imported)
        self.assertEqual(imported.packing_plan, self.packer.packing_plan)

    def test_old_json_without_rule_columns_loads_without_rules(self):
        data = json.loads(build_json_bytes(self.packer, ''))
        data.pop('strict_stack')
        data['boxes'] = [box[:9] for box in data['boxes']]
        imported, _ = load_saved_plan('old.json', json.dumps(data).encode('utf-8'))
        self.assertFalse(imported.strict_stack)
        self.assertFalse(any(imported.no_stack.values()))

    def test_cargo_xlsx_keeps_no_stack_column(self):
        rows = [{'l': 1.0, 'w': 1.0, 'h': 1.0, 'n': 1, 'wt': 10.0, 'type': '木箱',
                 'no_flip': False, 'prio': 0, 'desc': '普通', 'no_stack': False},
                {'l': 1.66, 'w': 1.3, 'h': 1.33, 'n': 1, 'wt': 170.0, 'type': '木箱',
                 'no_flip': True, 'prio': 0, 'desc': '内漏测试台', 'no_stack': True}]
        content = build_cargo_xlsx(CONTAINER_20GP, rows, 'HT2619C')
        data = parse_cargo_xlsx(io.BytesIO(content))
        self.assertEqual(data['no_stack'], [False, True])
        self.assertEqual(data['no_flip'], [False, True])
        self.assertEqual(len(parse_xlsx(io.BytesIO(content))), 6)  # 旧接口保持不变


if __name__ == '__main__':
    unittest.main()
