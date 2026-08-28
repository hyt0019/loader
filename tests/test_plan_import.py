import io
import sys
import types
import unittest

import openpyxl

# 这些测试只覆盖方案序列化，不需要启动 Streamlit/Plotly UI。
streamlit_stub = types.ModuleType('streamlit')
streamlit_stub.dialog = lambda *args, **kwargs: (lambda func: func)


class SessionState(dict):
    __getattr__ = dict.get

    def __setattr__(self, key, value):
        self[key] = value


streamlit_stub.session_state = SessionState()
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

from app import (build_cargo_xlsx, build_excel_bytes, build_json_bytes,
                 install_imported_rows, load_saved_plan, parse_xlsx)
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


class CargoListRoundTripTests(unittest.TestCase):
    def test_xlsx_preserves_checked_and_unchecked_no_flip_values(self):
        rows = [
            {'id': 1, 'l': 1.0, 'w': 0.5, 'h': 0.4, 'n': 2, 'wt': 10.0,
             'type': '纸箱', 'no_flip': False, 'prio': 0, 'desc': '未勾选'},
            {'id': 2, 'l': 0.8, 'w': 0.6, 'h': 0.3, 'n': 3, 'wt': 20.0,
             'type': '木箱', 'no_flip': True, 'prio': 7, 'desc': '已勾选'},
            # 字符串“否”必须按 False 处理，不能因为是非空字符串而导出成“是”。
            {'id': 3, 'l': 0.4, 'w': 0.3, 'h': 0.2, 'n': 1, 'wt': 5.0,
             'type': '托盘', 'no_flip': '否', 'prio': 2, 'desc': '字符串否'},
        ]
        content = build_cargo_xlsx((5800, 2350, 2350), rows, 'SO-CARGO-001')

        workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        sheet = workbook['货物清单']
        self.assertEqual([sheet.cell(row=row, column=8).value for row in range(5, 8)],
                         ['否', '是', '否'])
        workbook.close()

        (container, boxes, ship_no, descriptions,
         no_flip, priority) = parse_xlsx(io.BytesIO(content))
        self.assertEqual(container, (5800, 2350, 2350))
        self.assertEqual(ship_no, 'SO-CARGO-001')
        self.assertEqual(descriptions, ['未勾选', '已勾选', '字符串否'])
        self.assertEqual(no_flip, [False, True, False])
        self.assertEqual(priority, [0.0, 7.0, 2.0])

        streamlit_stub.session_state.clear()
        streamlit_stub.session_state.next_id = 0
        imported_rows = install_imported_rows(
            boxes, descriptions, no_flip, priority)
        self.assertEqual([row['no_flip'] for row in imported_rows], [False, True, False])
        self.assertEqual([row['prio'] for row in imported_rows], [0, 7, 2])
        self.assertEqual(
            [streamlit_stub.session_state[f"nf_{row['id']}"] for row in imported_rows],
            [False, True, False],
        )


if __name__ == '__main__':
    unittest.main()
