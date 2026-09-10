import sys
import types
import unittest
from collections import Counter
from itertools import permutations

# 算法回归不需要启动 Matplotlib 图形后端。
matplotlib_stub = types.ModuleType('matplotlib')
matplotlib_stub.use = lambda *args, **kwargs: None
matplotlib_pyplot_stub = types.ModuleType('matplotlib.pyplot')
matplotlib_patches_stub = types.ModuleType('matplotlib.patches')
matplotlib_patches_stub.Patch = object
mpl_toolkits_stub = types.ModuleType('mpl_toolkits')
mpl_toolkits_mplot3d_stub = types.ModuleType('mpl_toolkits.mplot3d')
mpl_toolkits_art3d_stub = types.ModuleType('mpl_toolkits.mplot3d.art3d')
mpl_toolkits_art3d_stub.Poly3DCollection = object
sys.modules.setdefault('matplotlib', matplotlib_stub)
sys.modules.setdefault('matplotlib.pyplot', matplotlib_pyplot_stub)
sys.modules.setdefault('matplotlib.patches', matplotlib_patches_stub)
sys.modules.setdefault('mpl_toolkits', mpl_toolkits_stub)
sys.modules.setdefault('mpl_toolkits.mplot3d', mpl_toolkits_mplot3d_stub)
sys.modules.setdefault('mpl_toolkits.mplot3d.art3d', mpl_toolkits_art3d_stub)

from packer_pro import ContainerPacker


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
        self.assert_valid_plan(enhanced)

    def test_column_beam_fits_high_utilization_mixed_customer_case(self):
        """高重复主货 + 多种少量辅货：验证通用混合列搜索，不依赖品名。"""
        boxes = [
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
        for first_type_no_flip in (True, False):
            with self.subTest(first_type_no_flip=first_type_no_flip):
                packer = ContainerPacker(
                    (5800, 2350, 2350), boxes,
                    no_flip=[first_type_no_flip, False, False, False, False,
                             False, False, False, False])
                fits, message, stats = packer.pack_enhanced(
                    time_budget=30, verbose=False)

                self.assertTrue(fits, message)
                self.assertEqual(stats['placed'], 613)
                self.assertTrue(stats['optimal_full'])
                self.assertEqual(stats['strategy'], 'column_beam_search')
                expected_volume = sum(l * w * h * quantity
                                      for l, w, h, quantity, _, _ in boxes)
                packed_volume = sum(l * w * h
                                    for _, _, _, l, w, h in packer.packing_plan)
                self.assertEqual(packed_volume, expected_volume)
                self.assertEqual(
                    Counter(info['input_order'] for info in packer.box_colors),
                    Counter({index: box[3] for index, box in enumerate(boxes)}),
                )
                self.assert_valid_plan(packer)

    def test_layer_column_strategy_fits_customer_case_with_all_rotations(self):
        boxes = [
            (405, 400, 405, 307, 17.5, '1'),
            (365, 365, 360, 27, 13.0, '1'),
            (460, 280, 580, 88, 11.4, '1'),
            (350, 310, 300, 1, 19.1, '1'),
            (460, 280, 580, 12, 5.5, '1'),
        ]
        packer = ContainerPacker(
            (5800, 2350, 2350), boxes, no_flip=[False] * len(boxes))
        fits, message, stats = packer.pack_enhanced(time_budget=10, verbose=False)

        self.assertTrue(fits, message)
        self.assertEqual(stats['placed'], 435)
        self.assertTrue(stats['optimal_full'])
        self.assertEqual(stats['strategy'], 'layer_columns')
        self.assertEqual(
            Counter(info['input_order'] for info in packer.box_colors),
            Counter({0: 307, 1: 27, 2: 88, 3: 1, 4: 12}),
        )
        allowed = {
            io: set(permutations(box[:3]))
            for io, box in enumerate(boxes)
        }
        for position, info in zip(packer.packing_plan, packer.box_colors):
            self.assertIn(position[3:], allowed[info['input_order']])
        self.assert_valid_plan(packer)

    def test_dense_column_strategy_improves_customer_case_and_is_valid(self):
        boxes = [
            (405, 400, 405, 307, 17.5, '1'),
            (365, 365, 360, 27, 13.0, '1'),
            (460, 280, 580, 88, 11.4, '1'),
            (350, 310, 300, 1, 19.1, '1'),
            (460, 280, 580, 12, 5.5, '1'),
        ]
        packer = ContainerPacker(
            (5800, 2350, 2350), boxes, no_flip=[True] * len(boxes))
        fits, message, stats = packer.pack_enhanced(time_budget=10, verbose=False)

        self.assertFalse(fits)
        self.assertIn('422/435', message)
        self.assertEqual(stats['placed'], 422)
        self.assertEqual(stats['strategy'], 'dense_columns')
        self.assertEqual(
            Counter(info['input_order'] for info in packer.box_colors),
            Counter({0: 307, 1: 26, 2: 88, 3: 1}),
        )
        self.assert_valid_plan(packer)

    def assert_valid_plan(self, packer):
        length, width, height = packer.container
        plan = packer.packing_plan
        for index, (x, y, z, box_length, box_width, box_height) in enumerate(plan):
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertGreaterEqual(z, 0)
            self.assertLessEqual(x + box_length, length)
            self.assertLessEqual(y + box_width, width)
            self.assertLessEqual(z + box_height, height)

            support = 0
            if z > 0:
                for ox, oy, oz, ol, ow, oh in plan:
                    if oz + oh != z:
                        continue
                    overlap_x = min(x + box_length, ox + ol) - max(x, ox)
                    overlap_y = min(y + box_width, oy + ow) - max(y, oy)
                    if overlap_x > 0 and overlap_y > 0:
                        support += overlap_x * overlap_y
                self.assertGreaterEqual(support * 5, box_length * box_width * 3)

            for other in plan[index + 1:]:
                ox, oy, oz, ol, ow, oh = other
                collision = (x < ox + ol and x + box_length > ox and
                             y < oy + ow and y + box_width > oy and
                             z < oz + oh and z + box_height > oz)
                self.assertFalse(collision)


if __name__ == '__main__':
    unittest.main()
