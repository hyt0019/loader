# -*- coding: utf-8 -*-
"""
装箱系统的通用逻辑：清单解析、导入导出、方案读写、实时预估与计算入口。

不依赖任何网页框架，由网站后端（server/）与 Streamlit 版（app.py）共用。
"""
import io
import json
import re

import numpy as np

from packer_pro import ContainerPacker, MM, to_mm, to_int, to_float


# --------------------------------------------------------------------------- #
#                                  数据解析                                  #
# --------------------------------------------------------------------------- #
def _extract_ship_no(text):
    """从文本中提取发货单号（匹配 `发货单号` 之后的内容，忽略前导 :：# 空白）。"""
    for ln in text.splitlines():
        if '发货单号' in ln:
            return ln.split('发货单号', 1)[1].lstrip(' :：#\t').strip()
    return ''


def _extract_desc_from_comment(raw):
    """从 txt 数据行的行末注释中提取零件描述（`描述：XXX` 或 `描述:XXX`）。"""
    if '#' not in raw:
        return ''
    comment = raw.split('#', 1)[1]
    for sep in ('描述：', '描述:'):
        if sep in comment:
            return comment.split(sep, 1)[1].strip()
    return ''


def _parse_bool(value, default=False):
    """把 Excel/会话中的常见布尔写法规范为 bool，避免非空字符串“否”被当成 True。"""
    if value is None:
        return bool(default)
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, float, np.integer, np.floating)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {'是', 'true', 'yes', 'y', '1', '勾选', '已勾选', '禁止'}:
        return True
    if text in {'否', 'false', 'no', 'n', '0', '', '未勾选', '允许'}:
        return False
    return bool(default)


def parse_txt(text):
    ship_no = _extract_ship_no(text)
    # 跳过空行与整行注释（# 开头），使带注释的导出文件可以原样再导入
    raw_lines = [ln for ln in text.splitlines()
                 if ln.strip() and ln.split('#')[0].strip()]
    container = tuple(to_mm(x) for x in raw_lines[0].split('#')[0].strip().split()[:3])
    box_count = int(raw_lines[1].split('#')[0].strip())
    boxes, descriptions = [], []
    for i in range(box_count):
        raw = raw_lines[2 + i]
        parts = raw.split('#')[0].strip().split()
        if len(parts) < 6:
            raise ValueError(f"第 {3 + i} 行数据列数不足（需要 长 宽 高 数量 重量 类型），实际: {parts}")
        boxes.append((to_mm(parts[0]), to_mm(parts[1]), to_mm(parts[2]),
                      to_int(parts[3]), to_float(parts[4]), parts[5]))
        descriptions.append(_extract_desc_from_comment(raw))
    return container, boxes, ship_no, descriptions


def parse_xlsx(file_like):
    import openpyxl
    wb = openpyxl.load_workbook(file_like, data_only=True)
    sheet = wb.active
    a1 = sheet.cell(row=1, column=1).value

    # 新版带标签格式：A1 == '发货单号'
    if isinstance(a1, str) and a1.strip() == '发货单号':
        ship_no = str(sheet.cell(row=1, column=2).value or '').strip()
        container = (to_mm(sheet.cell(row=2, column=3).value),
                     to_mm(sheet.cell(row=2, column=5).value),
                     to_mm(sheet.cell(row=2, column=7).value))
        # 定位表头行（第一列为「零件描述…」）
        hdr = None
        for rr in range(1, sheet.max_row + 1):
            v = sheet.cell(row=rr, column=1).value
            if isinstance(v, str) and v.startswith('零件描述'):
                hdr = rr
                break
        boxes, descriptions, no_flip, priority = [], [], [], []
        if hdr:
            for rr in range(hdr + 1, sheet.max_row + 1):
                l = sheet.cell(row=rr, column=2).value
                w = sheet.cell(row=rr, column=3).value
                h = sheet.cell(row=rr, column=4).value
                if to_mm(l) <= 0 or to_mm(w) <= 0 or to_mm(h) <= 0:
                    continue
                n = sheet.cell(row=rr, column=5).value
                wt = sheet.cell(row=rr, column=6).value
                tp = sheet.cell(row=rr, column=7).value
                code = TYPE_CODES.get(str(tp).strip(), str(tp).strip()) if tp is not None else '0'
                boxes.append((to_mm(l), to_mm(w), to_mm(h), to_int(n), to_float(wt), code))
                dv = sheet.cell(row=rr, column=1).value
                descriptions.append('' if dv is None else str(dv))
                no_flip.append(_parse_bool(sheet.cell(row=rr, column=8).value, False))
                priority.append(to_float(sheet.cell(row=rr, column=9).value, 0.0))
        wb.close()
        return container, boxes, ship_no, descriptions, no_flip, priority

    # 旧版定位格式（向后兼容）：第一行为集装箱长宽高
    container = tuple(to_mm(cell.value) for cell in list(sheet[1])[:3])
    sv = sheet.cell(row=1, column=5).value
    ship_no = '' if sv is None else str(sv).strip()
    box_count = int(sheet.cell(row=2, column=1).value)
    boxes = []
    for i in range(3, 3 + box_count):
        row = [cell.value for cell in sheet[i]]
        boxes.append((to_mm(row[0]), to_mm(row[1]), to_mm(row[2]),
                      to_int(row[3]), to_float(row[4]), str(row[5])))
    wb.close()
    return (container, boxes, ship_no, [''] * len(boxes),
            [False] * len(boxes), [0.0] * len(boxes))



# --------------------------------------------------------------------------- #
#                        货物类型 / 柜型 / 清单行互转                        #
# --------------------------------------------------------------------------- #
TYPE_LABELS = {'0': '木箱', '1': '纸箱', '2': '托盘'}

TYPE_CODES = {v: k for k, v in TYPE_LABELS.items()}

DF_COLUMNS = ['长(m)', '宽(m)', '高(m)', '数量', '重量(kg)', '类型']

def boxes_to_df(boxes):
    """内部 boxes(mm) -> 供表格展示的 DataFrame(米/中文类型)。"""
    import pandas as pd
    rows = []
    for (l, w, h, n, weight, t) in boxes:
        rows.append({'长(m)': l / MM, '宽(m)': w / MM, '高(m)': h / MM,
                     '数量': int(n), '重量(kg)': float(weight),
                     '类型': TYPE_LABELS.get(str(t), str(t))})
    return pd.DataFrame(rows, columns=DF_COLUMNS)


def df_to_boxes(df):
    """表格 DataFrame -> 内部 boxes(mm)，自动跳过空行/无效行。"""
    boxes = []
    for _, row in df.iterrows():
        l, w, h = to_mm(row['长(m)']), to_mm(row['宽(m)']), to_mm(row['高(m)'])
        n, weight = to_int(row['数量']), to_float(row['重量(kg)'])
        t = TYPE_CODES.get(str(row['类型']).strip(), str(row['类型']).strip())
        if l > 0 and w > 0 and h > 0 and n > 0:
            boxes.append((l, w, h, n, weight, t))
    return boxes


CARGO_TYPES = ['木箱', '纸箱', '托盘']

# 内置示例清单：(长, 宽, 高, 数量, 重量, 类型, 禁止倒放, 优先级, 零件描述)
SAMPLE_CARGO = [(0.49, 0.4, 0.09, 44, 18.0, '纸箱', False, 0, '法兰盘配件'),
                (1.2, 1.0, 1.35, 7, 1472.0, '木箱', False, 0, '主机箱体'),
                (0.6, 0.26, 0.37, 120, 12.0, '纸箱', False, 0, '连接管件'),
                (0.35, 0.28, 0.35, 168, 12.0, '纸箱', False, 0, '标准螺栓组'),
                (1.1, 1.1, 1.4, 2, 950.0, '木箱', False, 0, '变速箱总成')]
SAMPLE_CONTAINER = (5.8, 2.35, 2.35)  # 示例集装箱：20 尺柜近似内径（长×宽×高，米）

# 常用柜型内径（长×宽×高，米）——点选即可快速填入，仍可手动修改
CONTAINER_PRESETS = {
    '20GP': (5.8, 2.35, 2.35),
    '40GP': (12.0, 2.35, 2.35),
    '40HQ': (12.0, 2.35, 2.65),
}

def rows_to_boxes(rows):
    """货物行 -> 内部 boxes(mm)，自动跳过空行/无效行。"""
    out = []
    for r in rows:
        l, w, h = to_mm(r['l']), to_mm(r['w']), to_mm(r['h'])
        n, wt = to_int(r['n']), to_float(r['wt'])
        t = TYPE_CODES.get(r['type'], r['type'])
        if l > 0 and w > 0 and h > 0 and n > 0:
            out.append((l, w, h, n, wt, t))
    return out


def rows_to_boxes_ex(rows):
    """货物行 -> (boxes, no_flip, priority, descriptions)，四者按同一顺序对齐（供装箱器使用）。"""
    boxes, no_flip, priority, descriptions = [], [], [], []
    for r in rows:
        l, w, h = to_mm(r['l']), to_mm(r['w']), to_mm(r['h'])
        n, wt = to_int(r['n']), to_float(r['wt'])
        t = TYPE_CODES.get(r['type'], r['type'])
        if l > 0 and w > 0 and h > 0 and n > 0:
            boxes.append((l, w, h, n, wt, t))
            no_flip.append(_parse_bool(r.get('no_flip', False), False))
            priority.append(to_float(r.get('prio', 0)))
            descriptions.append(str(r.get('desc', '')))
    return boxes, no_flip, priority, descriptions


def _valid_rows(rows):
    return [r for r in rows
            if to_mm(r['l']) > 0 and to_mm(r['w']) > 0 and to_mm(r['h']) > 0 and to_int(r['n']) > 0]


def build_cargo_txt(container_mm, rows, ship_no=''):
    """把当前录入的集装箱+货物清单导出为 txt。

    格式与导入完全一致（行内 # 之后为注释，导入时会自动忽略），
    因此导出的文件可以直接用「从文件/文本导入」读回来，实现间断录入。
    发货单号写在首行注释 `# 发货单号: XXX`，导入时会自动读回。
    """
    valid = _valid_rows(rows)
    L, W, H = (c / MM for c in container_mm)
    lines = [f"# 发货单号: {ship_no}" if ship_no else "# 发货单号: （未填写）",
             f"{L:g} {W:g} {H:g}    # 集装箱内尺寸：长 宽 高（米）",
             f"{len(valid)}    # 货物种类数",
             "# 数据列：长(m) 宽(m) 高(m) 数量 重量(kg) 类型(0=木箱 1=纸箱 2=托盘)"
             "  ｜  行末为：类型 · 零件描述"]
    for r in valid:
        code = TYPE_CODES.get(r['type'], '0')
        label = TYPE_LABELS.get(code, str(r['type']))
        desc = str(r.get('desc', '')).strip()
        comment = label if not desc else f"{label} · 描述：{desc}"
        lines.append(f"{float(r['l']):g} {float(r['w']):g} {float(r['h']):g} "
                     f"{int(r['n'])} {float(r['wt']):g} {code}    # {comment}")
    return ("\n".join(lines) + "\n").encode('utf-8')


def build_cargo_xlsx(container_mm, rows, ship_no=''):
    """把当前录入的集装箱+货物清单导出为 xlsx（同样可直接再导入）。

    第一行 D/E 列存「发货单号」标签与值，导入时会自动读回；第7列的中文类型仅供阅读。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    valid = _valid_rows(rows)
    L, W, H = (c / MM for c in container_mm)
    wb = Workbook()
    ws = wb.active
    ws.title = '货物清单'

    bold = Font(bold=True)
    head_font = Font(bold=True, color='FFFFFF')
    head_fill = PatternFill('solid', fgColor='2E5395')
    label_fill = PatternFill('solid', fgColor='EEF3FB')
    center = Alignment(horizontal='center', vertical='center')
    thin = Side(style='thin', color='C9D4E6')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # 顶部信息区：发货单号 + 集装箱内尺寸（带清晰标签）
    ws['A1'] = '发货单号'; ws['B1'] = ship_no or '（未填写）'
    ws['A1'].font = bold; ws['A1'].fill = label_fill
    ws['A2'] = '集装箱内尺寸（米）'; ws['A2'].font = bold; ws['A2'].fill = label_fill
    ws['B2'] = '长'; ws['C2'] = L; ws['D2'] = '宽'; ws['E2'] = W; ws['F2'] = '高'; ws['G2'] = H
    for cellref in ('B2', 'D2', 'F2'):
        ws[cellref].font = bold

    # 表头（第 4 行）
    header = ['零件描述 / 品名', '长(m)', '宽(m)', '高(m)', '数量', '重量(kg)',
              '类型', '禁止倒放', '优先级']
    HEAD_ROW = 4
    for j, name in enumerate(header, start=1):
        c = ws.cell(row=HEAD_ROW, column=j, value=name)
        c.font = head_font; c.fill = head_fill; c.alignment = center; c.border = border

    # 数据行
    rrow = HEAD_ROW
    for r in valid:
        rrow += 1
        code = TYPE_CODES.get(r['type'], '0')
        label = TYPE_LABELS.get(code, str(r['type']))
        vals = [str(r.get('desc', '')), float(r['l']), float(r['w']), float(r['h']),
                int(r['n']), float(r['wt']), label,
                '是' if _parse_bool(r.get('no_flip'), False) else '否', int(r.get('prio', 0))]
        for j, v in enumerate(vals, start=1):
            c = ws.cell(row=rrow, column=j, value=v)
            c.border = border
            if j >= 2:
                c.alignment = center

    for idx, width in enumerate([26, 10, 10, 10, 8, 11, 9, 10, 8], start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.freeze_panes = f'A{HEAD_ROW + 1}'
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()



# --------------------------------------------------------------------------- #
#                                  装箱计算                                  #
# --------------------------------------------------------------------------- #
def run_packing(container, boxes, threshold, mode, budget,
                no_flip=None, priority=None, bottom_metric='none', descriptions=None):
    packer = ContainerPacker(container, boxes, threshold, no_flip=no_flip,
                             priority=priority, bottom_metric=bottom_metric,
                             descriptions=descriptions)
    if mode == "enhanced":
        fits, msg, stats = packer.pack_enhanced(time_budget=budget, verbose=False)
    else:
        fits, msg = packer.pack()
        used = sum(l * w * h for _, _, _, l, w, h in packer.packing_plan)
        stats = {
            'placed': len(packer.packing_plan), 'total': packer.total_units,
            'utilization': used / packer.container_volume if packer.container_volume else 0.0,
            'evaluations': 1, 'seconds': 0.0, 'optimal_full': fits,
        }
    return packer, fits, msg, stats


def baseline_note(container, boxes, threshold, constrained_placed):
    """当启用了「禁止倒放/底层优先」等约束、且未能全部装入时，
    对照「不加任何约束」的方案能装多少件，据实提示用户约束的代价（需求5）。

    对照只用块构建贪心（通常不到一秒），避免在计算时限之外再耗时完整求解一遍。"""
    base = ContainerPacker(container, boxes, threshold)  # 无约束，纯空间利用率
    candidate, _ = base._block_candidate()
    base_placed = candidate[0] if candidate else 0
    if base_placed > constrained_placed:
        return (f"提示：受「禁止倒放 / 底层优先」限制，本方案装入 {constrained_placed} 件；"
                f"若取消这些限制，至少可装 {base_placed} 件。"
                f"已在保留您所选限制的前提下尽力装载——如需装下更多，可放宽限制。")
    return None



# --------------------------------------------------------------------------- #
#                     导出：Excel / JSON 与已保存方案导入                    #
# --------------------------------------------------------------------------- #
def build_dataframe(packer):
    """明细装箱清单：按装载顺序列出每一件货的原始规格、摆放朝向与落位坐标。"""
    import pandas as pd
    rows = []
    for (x, y, z, l, w, h), info in sorted(zip(packer.packing_plan, packer.box_colors),
                                           key=lambda it: it[1]['number']):
        dl, dw, dh = info['original_dimensions']
        rows.append({
            '装载顺序': info['number'],
            '类别': info['input_order'] + 1,
            '零件描述': info.get('desc', ''),
            '类型': TYPE_LABELS.get(str(info['type']), str(info['type'])),
            '原始规格(长×宽×高 m)': f"{dl:g}×{dw:g}×{dh:g}",
            '摆放长(m)': l / MM, '摆放宽(m)': w / MM, '摆放高(m)': h / MM,
            '位置X起点(m)': x / MM, '位置Y起点(m)': y / MM, '位置Z起点(m)': z / MM,
            '重量(kg)': info['weight'],
        })
    return pd.DataFrame(rows)


def build_category_summary(packer):
    """分类汇总：每种货物的规格、应装/已装/未装数量与体积重量小计。"""
    import pandas as pd
    placed = {}
    for info in packer.box_colors:
        placed[info['input_order']] = placed.get(info['input_order'], 0) + 1
    rows = []
    for (l, w, h, n, weight, t, io) in packer.boxes:
        p = placed.get(io, 0)
        rows.append({
            '类别': io + 1,
            '零件描述': packer.descriptions.get(io, '') if hasattr(packer, 'descriptions') else '',
            '类型': TYPE_LABELS.get(str(t), str(t)),
            '规格(长×宽×高 m)': f"{l / MM:g}×{w / MM:g}×{h / MM:g}",
            '应装数量': n, '已装数量': p, '未装数量': n - p,
            '单件重量(kg)': weight,
            '禁止倒放': '是' if packer.no_flip.get(io, False) else '否',
            '优先级': packer.priority.get(io, 0),
            '已装重量(kg)': round(weight * p, 1),
            '单件体积(m³)': round(l * w * h / 1e9, 4),
            '已装体积(m³)': round(l * w * h * p / 1e9, 3),
        })
    return pd.DataFrame(rows)


def build_excel_bytes(packer, stats, ship_no=''):
    """导出三页 Excel：汇总 / 分类汇总 / 明细装箱清单。"""
    import pandas as pd
    from openpyxl.utils import get_column_letter
    detail = build_dataframe(packer)
    cats = build_category_summary(packer)
    used_vol = sum(l * w * h for _, _, _, l, w, h in packer.packing_plan) / 1e9
    total_weight = sum(info['weight'] for info in packer.box_colors)
    summary = pd.DataFrame({
        '项目': ['发货单号', '集装箱(长×宽×高 m)', '集装箱容积(m³)', '装入件数', '货物总件数', '未装件数',
                 '空间利用率', '已装货物体积(m³)', '已装货物总重(kg)', '搜索评估次数', '搜索用时(s)'],
        '数值': [
            ship_no or '—',
            f"{packer.container[0] / MM:g} × {packer.container[1] / MM:g} × {packer.container[2] / MM:g}",
            round(packer.container_volume / 1e9, 3),
            stats['placed'], stats['total'], stats['total'] - stats['placed'],
            f"{stats['utilization'] * 100:.1f}%",
            round(used_vol, 3), round(total_weight, 1),
            stats.get('evaluations', ''), stats.get('seconds', ''),
        ],
    })
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        # 「装箱清单」放首位：打开文件即是与页面一致的明细清单
        for name, frame in (('装箱清单', detail), ('分类汇总', cats), ('汇总', summary)):
            frame.to_excel(writer, sheet_name=name, index=False)
            ws = writer.sheets[name]
            for idx, col in enumerate(frame.columns, start=1):
                sample = [str(col)] + [str(v) for v in frame[col].head(300)]
                # 中文字符较宽，按 1.7 倍估算列宽
                width = min(max(max(len(s) for s in sample) * 1.7 + 2, 10), 32)
                ws.column_dimensions[get_column_letter(idx)].width = width
            ws.freeze_panes = 'A2'
        writer.book.active = 0  # 打开 Excel 时定位到「装箱清单」
    return buf.getvalue()


def build_json_bytes(packer, ship_no=''):
    data = {
        'ship_no': ship_no,
        'container': [c / MM for c in packer.container],
        'boxes': [[l / MM, w / MM, h / MM, n, weight, t,
                   packer.descriptions.get(i, ''),
                   bool(packer.no_flip.get(i, False)),
                   packer.priority.get(i, 0)]
                  for (l, w, h, n, weight, t, i) in packer.boxes],
        'packing_plan': [
            {'position': [p / MM for p in pos[:3]], 'dimensions': [p / MM for p in pos[3:]], 'box_info': info}
            for pos, info in zip(packer.packing_plan, packer.box_colors)],
    }
    return json.dumps(data, indent=4, ensure_ascii=False).encode('utf-8')


def load_plan_into_packer(plan):
    """从已保存的方案 JSON 重建 packer（仅用于查看，不重新计算）。"""
    container = tuple(to_mm(x) for x in plan['container'])
    boxes, descriptions, no_flip, priority = [], [], [], []
    for b in plan['boxes']:
        l, w, h, n, weight, t = b[:6]
        boxes.append((to_mm(l), to_mm(w), to_mm(h), int(n), float(weight), t))
        descriptions.append(b[6] if len(b) > 6 else '')
        # 第 8、9 列由新版方案保存；旧 JSON 没有这些列时保持原来的默认行为。
        no_flip.append(_parse_bool(b[7], False) if len(b) > 7 else False)
        priority.append(to_float(b[8], 0.0) if len(b) > 8 else 0.0)
    packer = ContainerPacker(container, boxes, no_flip=no_flip,
                             priority=priority, descriptions=descriptions)
    packer.packing_plan = [
        (to_mm(it['position'][0]), to_mm(it['position'][1]), to_mm(it['position'][2]),
         to_mm(it['dimensions'][0]), to_mm(it['dimensions'][1]), to_mm(it['dimensions'][2]))
        for it in plan['packing_plan']]
    packer.box_colors = [it['box_info'] for it in plan['packing_plan']]
    return packer


def _xlsx_table(ws, required_columns):
    """把网站导出的工作表转成按表头取值的记录，并校验必要列。"""
    header = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
    columns = {str(value).strip(): idx for idx, value in enumerate(header) if value is not None}
    missing = [name for name in required_columns if name not in columns]
    if missing:
        raise ValueError(f"工作表“{ws.title}”缺少列：{', '.join(missing)}")

    records = []
    for values in ws.iter_rows(min_row=2, values_only=True):
        if not any(value is not None and str(value).strip() for value in values):
            continue
        records.append({name: values[idx] if idx < len(values) else None
                        for name, idx in columns.items()})
    return records


def _xlsx_triplet(value, field_name, allow_zero=False):
    """解析网站 Excel 中的“长×宽×高”文本。"""
    numbers = re.findall(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?', str(value or ''))
    if len(numbers) != 3:
        raise ValueError(f'{field_name}无法识别：{value!r}')
    result = tuple(float(number) for number in numbers)
    if any(number < 0 if allow_zero else number <= 0 for number in result):
        raise ValueError(f'{field_name}包含无效尺寸：{value!r}')
    return result


def load_plan_xlsx(file_like):
    """从网站导出的 packing_plan.xlsx 重建 packer 和发货单号。"""
    import openpyxl

    wb = openpyxl.load_workbook(file_like, data_only=True, read_only=True)
    required_sheets = ('装箱清单', '分类汇总', '汇总')
    missing_sheets = [name for name in required_sheets if name not in wb.sheetnames]
    if missing_sheets:
        raise ValueError(f"不是网站导出的装箱方案，缺少工作表：{', '.join(missing_sheets)}")

    summary_rows = _xlsx_table(wb['汇总'], ('项目', '数值'))
    summary = {str(row['项目']).strip(): row['数值'] for row in summary_rows
               if row.get('项目') is not None}
    if '集装箱(长×宽×高 m)' not in summary:
        raise ValueError('汇总表缺少集装箱尺寸')
    container_m = _xlsx_triplet(summary['集装箱(长×宽×高 m)'], '集装箱尺寸')
    container = tuple(to_mm(value) for value in container_m)
    raw_ship_no = summary.get('发货单号')
    ship_no = '' if raw_ship_no in (None, '', '—', '（未填写）') else str(raw_ship_no).strip()

    category_rows = _xlsx_table(
        wb['分类汇总'],
        ('类别', '零件描述', '类型', '规格(长×宽×高 m)', '应装数量', '单件重量(kg)'),
    )
    if not category_rows:
        raise ValueError('分类汇总表没有货物数据')

    categories = []
    seen_categories = set()
    for row in category_rows:
        category = to_int(row['类别'])
        quantity = to_int(row['应装数量'])
        weight = to_float(row['单件重量(kg)'])
        if category <= 0 or category in seen_categories:
            raise ValueError(f"分类汇总表包含无效或重复类别：{row['类别']!r}")
        if quantity <= 0 or weight < 0:
            raise ValueError(f'类别 {category} 的数量或重量无效')
        seen_categories.add(category)
        dims_m = _xlsx_triplet(row['规格(长×宽×高 m)'], f'类别 {category} 的规格')
        type_text = str(row['类型'] or '0').strip()
        type_code = TYPE_CODES.get(type_text, type_text)
        description = '' if row['零件描述'] is None else str(row['零件描述'])
        no_flip = _parse_bool(row.get('禁止倒放'), False)
        priority = to_float(row.get('优先级'), 0.0)
        categories.append((category, dims_m, quantity, weight, type_code,
                           description, no_flip, priority))

    categories.sort(key=lambda item: item[0])
    expected = list(range(1, len(categories) + 1))
    actual = [item[0] for item in categories]
    if actual != expected:
        raise ValueError(f'分类汇总表的类别编号应从 1 连续排列，实际为：{actual}')

    boxes = [(to_mm(dims[0]), to_mm(dims[1]), to_mm(dims[2]), quantity, weight, type_code)
             for _, dims, quantity, weight, type_code, _, _, _ in categories]
    descriptions = [description for _, _, _, _, _, description, _, _ in categories]
    no_flip = [flag for _, _, _, _, _, _, flag, _ in categories]
    priority = [value for _, _, _, _, _, _, _, value in categories]
    packer = ContainerPacker(container, boxes, no_flip=no_flip,
                             priority=priority, descriptions=descriptions)

    detail_rows = _xlsx_table(
        wb['装箱清单'],
        ('装载顺序', '类别', '零件描述', '类型', '摆放长(m)', '摆放宽(m)', '摆放高(m)',
         '位置X起点(m)', '位置Y起点(m)', '位置Z起点(m)', '重量(kg)'),
    )
    detail_rows.sort(key=lambda row: to_int(row['装载顺序']))
    type_counts = {}
    for row in detail_rows:
        number = to_int(row['装载顺序'])
        category = to_int(row['类别'])
        if number <= 0 or category not in seen_categories:
            raise ValueError(f'装箱清单包含无效的装载顺序或类别：{number}, {category}')
        placed_m = tuple(to_float(row[name]) for name in ('摆放长(m)', '摆放宽(m)', '摆放高(m)'))
        position_m = tuple(to_float(row[name]) for name in ('位置X起点(m)', '位置Y起点(m)', '位置Z起点(m)'))
        if any(value <= 0 for value in placed_m) or any(value < 0 for value in position_m):
            raise ValueError(f'装载顺序 {number} 的摆放尺寸或位置无效')

        (_, original_m, _, category_weight, type_code, category_desc,
         _, _) = categories[category - 1]
        type_counts[type_code] = type_counts.get(type_code, 0) + 1
        packer.packing_plan.append(tuple(to_mm(value) for value in position_m + placed_m))
        packer.box_colors.append({
            'number': number,
            'type': type_code,
            'type_count': type_counts[type_code],
            'weight': to_float(row['重量(kg)'], category_weight),
            'input_order': category - 1,
            'original_dimensions': original_m,
            'desc': category_desc if row['零件描述'] is None else str(row['零件描述']),
        })
    return packer, ship_no


def load_saved_plan(file_name, content):
    """按扩展名读取网站导出的 JSON 或 XLSX 方案。"""
    lower_name = str(file_name).lower()
    if lower_name.endswith('.json'):
        plan = json.loads(content.decode('utf-8-sig'))
        return load_plan_into_packer(plan), str(plan.get('ship_no', '') or '')
    if lower_name.endswith('.xlsx'):
        return load_plan_xlsx(io.BytesIO(content))
    raise ValueError('仅支持网站导出的 JSON 或 XLSX 方案文件')


def saved_plan_form_data(packer, ship_no=''):
    """提取方案中的原始输入，供下一轮 Streamlit 渲染前恢复表单。

    使用 ``packer.boxes`` 的应装数量，而不是从已装明细反推，因此未装进去的
    货物也会完整回到清单中。
    """
    boxes = [tuple(box[:6]) for box in packer.boxes]
    input_orders = [box[6] for box in packer.boxes]
    return {
        'container': tuple(packer.container),
        'boxes': boxes,
        'ship_no': str(ship_no or ''),
        'descriptions': [packer.descriptions.get(io, '') for io in input_orders],
        'no_flip': [bool(packer.no_flip.get(io, False)) for io in input_orders],
        'priority': [packer.priority.get(io, 0) for io in input_orders],
    }



# --------------------------------------------------------------------------- #
#                                  实时预估                                  #
# --------------------------------------------------------------------------- #
def compute_estimate(container_mm, boxes, threshold):
    """根据当前集装箱与货物清单，计算体积/重量预估与潜在问题（不依赖界面）。"""
    cont_vol = container_mm[0] * container_mm[1] * container_mm[2]
    cargo_vol = sum(b[0] * b[1] * b[2] * b[3] for b in boxes)
    if cont_vol > 0:
        cd = sorted(container_mm)
        oversized = [b for b in boxes
                     if not all(sorted((b[0], b[1], b[2]))[i] <= cd[i] for i in range(3))]
    else:
        oversized = []  # 集装箱尚未填写，暂不判断超尺寸
    heavy = [b for b in boxes if b[4] > threshold]
    return {
        'pieces': sum(b[3] for b in boxes),
        'cargo_vol_m3': cargo_vol / 1e9,
        'cont_vol_m3': cont_vol / 1e9,
        'ratio': (cargo_vol / cont_vol) if cont_vol > 0 else 0.0,
        'weight': sum(b[4] * b[3] for b in boxes),
        'over_volume': cont_vol > 0 and cargo_vol > cont_vol,
        'oversized_count': len(oversized),
        'heavy_count': len(heavy),
    }
