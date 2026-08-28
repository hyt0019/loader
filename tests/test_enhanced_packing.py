import sys
import types
import unittest
from collections import Counter

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
