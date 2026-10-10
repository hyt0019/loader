import sys
import time
import unittest
from collections import Counter
from itertools import permutations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from block_engine import BlockSearch, center_supported  # noqa: E402
from packer_pro import ContainerPacker  # noqa: E402

MIXED_CUSTOMER = [
    (365, 365, 350, 380, 15.0, 'A'),
    (365, 365, 350, 3, 9.825, 'B'),
    (400, 360, 270, 2, 8.1, 'C'),
    (300, 300, 250, 1, 6.8, 'D'),
    (320, 280, 160, 10, 15.0, 'E'),
    (350, 280, 350, 80, 12.0, 'F'),
    (460, 330, 210, 4, 13.2, 'G'),
    (365, 365, 350, 131, 9.02, 'H'),
    (540, 340, 240, 2, 10.0, 'I'),
]
COLUMN_CUSTOMER = [
    (405, 400, 405, 307, 17.5, '1'),
    (365, 365, 360, 27, 13.0, '1'),
    (460, 280, 580, 88, 11.4, '1'),
    (350, 310, 300, 1, 19.1, '1'),
    (460, 280, 580, 12, 5.5, '1'),
]


class CenterSupportRuleTests(unittest.TestCase):
    """重心支撑：底面中心必须严格落在下方接触面的凸包内。"""

    def test_box_on_identical_box_is_supported(self):
        self.assertTrue(center_supported(0, 0, 100, 100, [(0, 0, 100, 100)]))

    def test_center_over_air_is_not_supported(self):
        # 只压住左边 40%：中心悬空
        self.assertFalse(center_supported(0, 0, 100, 100, [(0, 0, 40, 100)]))

    def test_center_on_outer_edge_is_not_supported(self):
        # 中心恰好落在唯一支撑物的外边缘上：会翻倒
        self.assertFalse(center_supported(0, 0, 100, 100, [(0, 0, 50, 100)]))

    def test_bridge_over_two_boxes_is_supported(self):
        # 横跨两个箱子、中心落在中间空隙上方：位于凸包内，稳定
        self.assertTrue(center_supported(0, 0, 100, 100, [(0, 0, 30, 100), (70, 0, 100, 100)]))

    def test_center_on_seam_between_two_boxes_is_supported(self):
        self.assertTrue(center_supported(0, 0, 100, 100, [(0, 0, 50, 100), (50, 0, 100, 100)]))

    def test_odd_sizes_use_exact_half_millimetre_center(self):
        # 边长 101：中心在 50.5，支撑到 51 为止仍在内部，到 50 为止则悬空
        self.assertTrue(center_supported(0, 0, 101, 101, [(0, 0, 51, 51)]))
        self.assertFalse(center_supported(0, 0, 101, 101, [(0, 0, 50, 101)]))

    def test_kernel_rejects_floating_box(self):
        packer = ContainerPacker((1000, 1000, 1000), [(200, 200, 200, 2, 1.0, '0')])
        packer._place(0, 0, 0, 200, 200, 200)
        self.assertFalse(packer.can_place(150, 0, 200, 200, 200, 200, 1.0))  # 中心在 250：悬空
        self.assertTrue(packer.can_place(50, 0, 200, 200, 200, 200, 1.0))    # 中心在 150：有支撑
        self.assertFalse(packer.can_place(0, 0, 500, 200, 200, 200, 1.0))    # 完全悬空


class BlockSearchTests(unittest.TestCase):
    def test_greedy_plan_is_valid_and_respects_quantities(self):
        packer = ContainerPacker((5800, 2350, 2350), MIXED_CUSTOMER)
        candidate, _ = packer._block_candidate()
        self.assertIsNotNone(candidate)
        assert_valid_plan(self, packer)
        counts = Counter(info['input_order'] for info in packer.box_colors)
        for index, box in enumerate(MIXED_CUSTOMER):
            self.assertLessEqual(counts[index], box[3])

    def test_no_flip_keeps_original_height(self):
        boxes = [(600, 400, 300, 40, 5.0, '1'), (500, 300, 800, 20, 9.0, '0')]
        packer = ContainerPacker((2400, 2000, 2000), boxes, no_flip=[True, True])
        packer._block_candidate(deadline=time.time() + 1)
        assert_valid_plan(self, packer)
        for position, info in zip(packer.packing_plan, packer.box_colors):
            self.assertEqual(position[5], boxes[info['input_order']][2])

    def test_beam_search_is_never_worse_than_greedy(self):
        kinds = [([(300, 200, 150), (200, 300, 150)], 40), ([(450, 350, 250)], 12),
                 ([(120, 120, 400), (400, 120, 120), (120, 400, 120)], 30)]
        search = BlockSearch((1600, 1100, 1000), kinds)
        greedy = search.greedy()
        best, _ = search.beam_search(time.time() + 1.5)
        self.assertGreaterEqual(best.volume, greedy.volume)

    def test_standard_is_never_worse_than_original_box_by_box_policies(self):
        cases = [
            ((1200, 1000, 1000), [(310, 250, 300, 15, 8.0, 'A'), (280, 220, 350, 12, 6.0, 'B'),
                                  (190, 170, 200, 20, 3.0, 'C')]),
            ((5800, 2350, 2350), COLUMN_CUSTOMER),
        ]
        for container, boxes in cases:
            with self.subTest(container=container):
                original = ContainerPacker(container, boxes)
                original_used = max(
                    original._solve(swap, pol[0], pol[1], record=False)[2]
                    for swap in (False, True) for pol in original._deterministic_policies())
                standard = ContainerPacker(container, boxes)
                standard.pack()
                used = sum(l * w * h for _, _, _, l, w, h in standard.packing_plan)
                self.assertGreaterEqual(used, original_used)
                assert_valid_plan(self, standard)


class EnhancedPackingRegressionTests(unittest.TestCase):
    """客户 GT10B-2026 清单的匿名几何回归（整数毫米）。"""

    def test_enhanced_keeps_standard_result_as_elite_lower_bound(self):
        boxes = [
            (310, 250, 300, 15, 8.0, 'A'),
            (280, 220, 350, 12, 6.0, 'B'),
            (190, 170, 200, 20, 3.0, 'C'),
        ]
        standard = ContainerPacker((1200, 1000, 1000), boxes)
        standard.pack()

        enhanced = ContainerPacker((1200, 1000, 1000), boxes)
        _, _, stats = enhanced.pack_enhanced(time_budget=0.25, verbose=False)

        standard_used = sum(l * w * h for _, _, _, l, w, h in standard.packing_plan)
        enhanced_used = sum(l * w * h for _, _, _, l, w, h in enhanced.packing_plan)
        self.assertGreaterEqual(enhanced_used, standard_used)
        assert_valid_plan(self, enhanced)

    def test_enhanced_fits_high_utilization_mixed_customer_case(self):
        """高重复主货 + 多种少量辅货：613 件必须全部装入。"""
        for first_type_no_flip in (True, False):
            with self.subTest(first_type_no_flip=first_type_no_flip):
                packer = ContainerPacker(
                    (5800, 2350, 2350), MIXED_CUSTOMER,
                    no_flip=[first_type_no_flip] + [False] * 8)
                fits, message, stats = packer.pack_enhanced(time_budget=30, verbose=False)

                self.assertTrue(fits, message)
                self.assertEqual(stats['placed'], 613)
                self.assertTrue(stats['optimal_full'])
                expected_volume = sum(l * w * h * quantity
                                      for l, w, h, quantity, _, _ in MIXED_CUSTOMER)
                packed_volume = sum(l * w * h
                                    for _, _, _, l, w, h in packer.packing_plan)
                self.assertEqual(packed_volume, expected_volume)
                self.assertEqual(
                    Counter(info['input_order'] for info in packer.box_colors),
                    Counter({index: box[3] for index, box in enumerate(MIXED_CUSTOMER)}),
                )
                assert_valid_plan(self, packer)

    def test_column_beam_strategy_still_fits_mixed_customer_case(self):
        packer = ContainerPacker((5800, 2350, 2350), MIXED_CUSTOMER, no_flip=[True] + [False] * 8)
        candidate = packer._column_beam_candidate(deadline=time.time() + 30, beam_width=420)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate[0], 613)
        assert_valid_plan(self, packer)

    def test_enhanced_fits_customer_case_with_all_rotations(self):
        packer = ContainerPacker(
            (5800, 2350, 2350), COLUMN_CUSTOMER, no_flip=[False] * len(COLUMN_CUSTOMER))
        fits, message, stats = packer.pack_enhanced(time_budget=10, verbose=False)

        self.assertTrue(fits, message)
        self.assertEqual(stats['placed'], 435)
        self.assertTrue(stats['optimal_full'])
        self.assertEqual(
            Counter(info['input_order'] for info in packer.box_colors),
            Counter({0: 307, 1: 27, 2: 88, 3: 1, 4: 12}),
        )
        allowed = {
            io: set(permutations(box[:3]))
            for io, box in enumerate(COLUMN_CUSTOMER)
        }
        for position, info in zip(packer.packing_plan, packer.box_colors):
            self.assertIn(position[3:], allowed[info['input_order']])
        assert_valid_plan(self, packer)

    def test_layer_column_strategy_still_fits_customer_case(self):
        packer = ContainerPacker((5800, 2350, 2350), COLUMN_CUSTOMER, no_flip=[False] * 5)
        candidate = packer._layer_column_candidate(deadline=time.time() + 10)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate[0], 435)
        assert_valid_plan(self, packer)

    def test_enhanced_no_flip_customer_case_is_at_least_previous_best(self):
        # 旧算法（列生成）在全部禁止倒放时最多装入 422/435
        packer = ContainerPacker(
            (5800, 2350, 2350), COLUMN_CUSTOMER, no_flip=[True] * len(COLUMN_CUSTOMER))
        fits, message, stats = packer.pack_enhanced(time_budget=10, verbose=False)

        self.assertGreaterEqual(stats['placed'], 422, message)
        for position, info in zip(packer.packing_plan, packer.box_colors):
            self.assertEqual(position[5], COLUMN_CUSTOMER[info['input_order']][2])
        assert_valid_plan(self, packer)

    def test_dense_column_strategy_is_valid(self):
        packer = ContainerPacker((5800, 2350, 2350), COLUMN_CUSTOMER, no_flip=[True] * 5)
        candidate = packer._dense_column_candidate(deadline=time.time() + 10)
        self.assertIsNotNone(candidate)
        self.assertGreaterEqual(candidate[0], 422)
        assert_valid_plan(self, packer)


def assert_valid_plan(case, packer):
    """独立复核：不出界、不重叠、非落地货物重心有支撑。"""
    length, width, height = packer.container
    plan = packer.packing_plan
    for index, (x, y, z, box_length, box_width, box_height) in enumerate(plan):
        case.assertGreaterEqual(x, 0)
        case.assertGreaterEqual(y, 0)
        case.assertGreaterEqual(z, 0)
        case.assertLessEqual(x + box_length, length)
        case.assertLessEqual(y + box_width, width)
        case.assertLessEqual(z + box_height, height)
        if z > 0:
            tops = [(ox, oy, ox + ol, oy + ow) for ox, oy, oz, ol, ow, oh in plan if oz + oh == z]
            case.assertTrue(center_supported(x, y, box_length, box_width, tops),
                            f'第 {index + 1} 件货物重心悬空')
        for other in plan[index + 1:]:
            ox, oy, oz, ol, ow, oh = other
            collision = (x < ox + ol and x + box_length > ox and
                         y < oy + ow and y + box_width > oy and
                         z < oz + oh and z + box_height > oz)
            case.assertFalse(collision)


if __name__ == '__main__':
    unittest.main()
