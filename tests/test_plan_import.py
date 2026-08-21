import sys
import types
import unittest

# 这些测试只覆盖方案序列化，不需要启动 Streamlit/Plotly UI。
streamlit_stub = types.ModuleType('streamlit')
streamlit_stub.dialog = lambda *args, **kwargs: (lambda func: func)
plotly_stub = types.ModuleType('plotly')
plotly_graph_objects_stub = types.ModuleType('plotly.graph_objects')
plotly_stub.graph_objects = plotly_graph_objects_stub
matplotlib_stub = types.ModuleType('matplotlib')
matplotlib_stub.use = lambda *args, **kwargs: None
matplotlib_pyplot_stub = types.ModuleType('matplotlib.pyplot')
matplotlib_patches_stub = types.ModuleType('matplotlib.patches')
matplotlib_patches_stub.Patch = object
mpl_toolkits_stub = types.ModuleType('mpl_toolkits')
mpl_toolkits_mplot3d_stub = types.ModuleType('mpl_toolkits.mplot3d')
mpl_toolkits_art3d_stub = types.ModuleType('mpl_toolkits.mplot3d.art3d')
mpl_toolkits_art3d_stub.Poly3DCollection = object
sys.modules.setdefault('streamlit', streamlit_stub)
sys.modules.setdefault('plotly', plotly_stub)
sys.modules.setdefault('plotly.graph_objects', plotly_graph_objects_stub)
sys.modules.setdefault('matplotlib', matplotlib_stub)
sys.modules.setdefault('matplotlib.pyplot', matplotlib_pyplot_stub)
sys.modules.setdefault('matplotlib.patches', matplotlib_patches_stub)
sys.modules.setdefault('mpl_toolkits', mpl_toolkits_stub)
sys.modules.setdefault('mpl_toolkits.mplot3d', mpl_toolkits_mplot3d_stub)
sys.modules.setdefault('mpl_toolkits.mplot3d.art3d', mpl_toolkits_art3d_stub)

from app import build_excel_bytes, build_json_bytes, load_saved_plan
from packer_pro import ContainerPacker, MM


class SavedPlanImportTests(unittest.TestCase):
    def setUp(self):
        self.packer = ContainerPacker(
            (2 * MM, 2 * MM, 2 * MM),
            [(1 * MM, 0.5 * MM, 0.5 * MM, 2, 12.5, '1'),
             (0.5 * MM, 0.5 * MM, 0.5 * MM, 1, 30.0, '0')],
            descriptions=['纸箱零件', '木箱零件'],
        )
        self.packer.packing_plan = [
            (0, 0, 0, 1 * MM, 0.5 * MM, 0.5 * MM),
            (1 * MM, 0, 0, 1 * MM, 0.5 * MM, 0.5 * MM),
            (0, 0.5 * MM, 0, 0.5 * MM, 0.5 * MM, 0.5 * MM),
        ]
        self.packer.box_colors = [
            {'number': 1, 'type': '1', 'type_count': 1, 'weight': 12.5,
             'input_order': 0, 'original_dimensions': (1.0, 0.5, 0.5), 'desc': '纸箱零件'},
            {'number': 2, 'type': '1', 'type_count': 2, 'weight': 12.5,
             'input_order': 0, 'original_dimensions': (1.0, 0.5, 0.5), 'desc': '纸箱零件'},
            {'number': 3, 'type': '0', 'type_count': 1, 'weight': 30.0,
             'input_order': 1, 'original_dimensions': (0.5, 0.5, 0.5), 'desc': '木箱零件'},
        ]
        self.stats = {
            'placed': 3, 'total': 3, 'utilization': 0.078125,
            'evaluations': 1, 'seconds': 0.0,
        }

    def assert_round_trip(self, imported, ship_no):
        self.assertEqual(ship_no, 'SO-TEST-001')
        self.assertEqual(imported.container, self.packer.container)
        self.assertEqual(imported.boxes, self.packer.boxes)
        self.assertEqual(imported.packing_plan, self.packer.packing_plan)
        self.assertEqual([info['number'] for info in imported.box_colors], [1, 2, 3])
        self.assertEqual([info['input_order'] for info in imported.box_colors], [0, 0, 1])
        self.assertEqual([info['desc'] for info in imported.box_colors], ['纸箱零件', '纸箱零件', '木箱零件'])

    def test_json_export_can_be_imported(self):
        content = build_json_bytes(self.packer, 'SO-TEST-001')
        imported, ship_no = load_saved_plan('packing_plan.json', content)
        self.assert_round_trip(imported, ship_no)

    def test_website_xlsx_export_can_be_imported(self):
        content = build_excel_bytes(self.packer, self.stats, 'SO-TEST-001')
        imported, ship_no = load_saved_plan('packing_plan.xlsx', content)
        self.assert_round_trip(imported, ship_no)


if __name__ == '__main__':
    unittest.main()
