# -*- coding: utf-8 -*-
"""计算子进程的入口：只加载装箱所需的模块，保持子进程启动快、占用小。"""
import json

from packer_pro import to_mm
from planner_core import baseline_note, build_json_bytes, rows_to_boxes_ex, run_packing


def compute_plan(params):
    """按网页提交的参数装箱，返回可直接发给浏览器的结果。

    结果中的 ``plan`` 与「下载 JSON 方案」的格式完全一致，可原样导出、再导入。
    """
    container = tuple(to_mm(value) for value in params['container'])
    boxes, no_flip, priority, descriptions = rows_to_boxes_ex(params['rows'])
    mode = params['mode']
    bottom_metric = params['bottom_metric']
    threshold = params['threshold']
    packer, fits, msg, stats = run_packing(
        container, boxes, threshold, mode, params['budget'],
        no_flip=no_flip, priority=priority, bottom_metric=bottom_metric,
        descriptions=descriptions)
    note = None
    # 约束导致装不满时，对照无约束方案并提示代价
    if not fits and (any(no_flip) or bottom_metric != 'none'):
        note = baseline_note(container, boxes, threshold, stats['placed'])
    plan = json.loads(build_json_bytes(packer, params.get('ship_no', '')))
    return {'plan': plan, 'stats': stats, 'fits': fits, 'msg': msg, 'note': note, 'mode': mode}


def run_job(params, conn):
    """子进程主函数：把结果或错误通过管道交回主进程。"""
    try:
        conn.send(('done', compute_plan(params)))
    except Exception as exc:  # noqa: BLE001 —— 任何计算异常都要交回给网页显示
        conn.send(('error', f'{type(exc).__name__}: {exc}'))
    finally:
        conn.close()
