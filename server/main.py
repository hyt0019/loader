# -*- coding: utf-8 -*-
"""装箱系统网站后端（FastAPI）。

前端是 web/ 下的静态页面，三维查看、高亮、分段等交互全部在浏览器中完成；
后端只负责：装箱计算（子进程任务）、清单与方案文件的解析和生成。

本地运行：
    pip install -r requirements-server.txt
    python -m uvicorn server.main:app --port 8000
"""
import asyncio
import io
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Literal, Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.gzip import GZipMiddleware

from packer_pro import MM, to_mm
from planner_core import (CARGO_TYPES, CONTAINER_PRESETS, SAMPLE_CARGO, SAMPLE_CONTAINER,
                          TYPE_LABELS, build_cargo_txt, build_cargo_xlsx, build_excel_bytes,
                          build_json_bytes, load_plan_into_packer, load_saved_plan, parse_txt,
                          parse_xlsx, saved_plan_form_data)
from server.jobs import JobManager, JobRejected

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / 'web'


def _env_number(name, default, cast=int):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


MAX_BUDGET = _env_number('LOADER_MAX_BUDGET', 600, float)        # 增强版搜索时间上限（秒）
MAX_PIECES = _env_number('LOADER_MAX_PIECES', 20000)              # 单次计算最多件数
MAX_UPLOAD = _env_number('LOADER_MAX_UPLOAD_MB', 10) * 1024 * 1024

jobs = JobManager(
    max_running=_env_number('LOADER_MAX_RUNNING', os.cpu_count() or 2),
    max_queued=_env_number('LOADER_MAX_QUEUED', 20),
    max_per_client=_env_number('LOADER_MAX_PER_CLIENT', 2),
    # 硬性时限 = 增强版搜索上限 + 该余量（标准版只有余量），防止异常任务一直占用 CPU
    extra_seconds=_env_number('LOADER_JOB_EXTRA_SECONDS', 600, float),
)


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(jobs.supervise())
    try:
        yield
    finally:
        task.cancel()
        jobs.shutdown()


app = FastAPI(title='HYT Container Load Planner', lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1024)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; object-src 'none'; "
       "base-uri 'self'; form-action 'self'; frame-ancestors 'self'")


@app.middleware('http')
async def cache_and_security_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith('/api/'):
        response.headers.setdefault('Cache-Control', 'no-store')
    elif path.startswith('/vendor/'):
        # 第三方库文件名带版本号，内容不会变化，可长期缓存
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    else:
        # 页面与脚本每次都向服务器确认是否更新（未更新时只返回 304，几乎不耗流量）
        response.headers['Cache-Control'] = 'no-cache'
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Content-Security-Policy', CSP)
    return response


# --------------------------------------------------------------------------- #
#                                 数据模型                                     #
# --------------------------------------------------------------------------- #
class CargoRow(BaseModel):
    """货物清单中的一行（尺寸单位：米）。"""
    model_config = ConfigDict(extra='ignore')

    l: float = Field(0.0, ge=0, le=100)
    w: float = Field(0.0, ge=0, le=100)
    h: float = Field(0.0, ge=0, le=100)
    n: int = Field(1, ge=0, le=1_000_000)
    wt: float = Field(0.0, ge=0, le=1_000_000)
    type: str = '木箱'
    no_flip: bool = False
    prio: float = Field(0.0, ge=-1_000_000, le=1_000_000)
    desc: str = Field('', max_length=200)

    @field_validator('type')
    @classmethod
    def _known_type(cls, value):
        value = TYPE_LABELS.get(str(value).strip(), str(value).strip())
        if value not in CARGO_TYPES:
            raise ValueError(f'未知的货物类型：{value}')
        return value


class CargoList(BaseModel):
    container: List[float] = Field(..., min_length=3, max_length=3)
    ship_no: str = Field('', max_length=100)
    rows: List[CargoRow] = Field(default_factory=list, max_length=500)

    @field_validator('container')
    @classmethod
    def _container_range(cls, value):
        if any(v < 0 or v > 100 for v in value):
            raise ValueError('集装箱尺寸应在 0 ~ 100 米之间')
        return value


class JobRequest(CargoList):
    mode: Literal['standard', 'enhanced'] = 'standard'
    budget: float = Field(600.0, ge=10, le=600)
    bottom_metric: Literal['none', 'weight', 'volumetric', 'manual'] = 'none'
    threshold: float = Field(100.0, ge=0, le=1_000_000)


class PlanExport(BaseModel):
    plan: dict
    stats: dict = Field(default_factory=dict)
    ship_no: str = Field('', max_length=100)


# --------------------------------------------------------------------------- #
#                                 工具函数                                     #
# --------------------------------------------------------------------------- #
def _rows_from_boxes(boxes, descriptions=None, no_flip=None, priority=None):
    """内部 boxes(mm) -> 网页货物行(米、中文类型)。"""
    descriptions, no_flip, priority = descriptions or [], no_flip or [], priority or []
    rows = []
    for i, (l, w, h, n, weight, t) in enumerate(boxes):
        rows.append({
            'l': l / MM, 'w': w / MM, 'h': h / MM, 'n': int(n), 'wt': float(weight),
            'type': TYPE_LABELS.get(str(t), str(t)),
            'no_flip': bool(no_flip[i]) if i < len(no_flip) else False,
            'prio': float(priority[i]) if i < len(priority) else 0.0,
            'desc': str(descriptions[i]) if i < len(descriptions) else '',
        })
    return rows


def _download(content, filename, media_type):
    return Response(content, media_type=media_type, headers={
        'Content-Disposition': f"attachment; filename*=UTF-8''{quote(filename)}"})


async def _read_upload(upload: UploadFile):
    content = await upload.read(MAX_UPLOAD + 1)
    if len(content) > MAX_UPLOAD:
        raise HTTPException(413, f'文件过大（上限 {MAX_UPLOAD // 1024 // 1024} MB）')
    return content


def _decode_text(content):
    """TXT 清单可能来自记事本（UTF-8）或旧版 Windows 软件（GBK），依次尝试。"""
    for encoding in ('utf-8-sig', 'gbk'):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise HTTPException(400, '无法识别文件编码，请另存为 UTF-8 后重试')


def _plan_stats(packer, client_stats=None):
    """根据方案本身统计装入件数与利用率；搜索次数 / 用时沿用计算时的记录。"""
    client_stats = client_stats or {}
    used = sum(l * w * h for _, _, _, l, w, h in packer.packing_plan)
    return {
        'placed': len(packer.packing_plan),
        'total': packer.total_units,
        'utilization': used / packer.container_volume if packer.container_volume else 0.0,
        'evaluations': client_stats.get('evaluations', ''),
        'seconds': client_stats.get('seconds', ''),
    }


# --------------------------------------------------------------------------- #
#                                   接口                                       #
# --------------------------------------------------------------------------- #
@app.get('/api/health')
async def health():
    return {'status': 'ok'}


@app.get('/api/config')
async def config():
    sample_rows = [{'l': l, 'w': w, 'h': h, 'n': n, 'wt': wt, 'type': t,
                    'no_flip': nf, 'prio': prio, 'desc': desc}
                   for (l, w, h, n, wt, t, nf, prio, desc) in SAMPLE_CARGO]
    return {
        'presets': {name: list(dims) for name, dims in CONTAINER_PRESETS.items()},
        'sample': {'container': list(SAMPLE_CONTAINER), 'rows': sample_rows},
        'cargo_types': CARGO_TYPES,
        'max_budget': MAX_BUDGET,
        'max_pieces': MAX_PIECES,
    }


@app.post('/api/jobs', status_code=202)
async def create_job(req: JobRequest, request: Request):
    if min(req.container) <= 0:
        raise HTTPException(400, '请填写有效的集装箱尺寸（长/宽/高均需大于 0）。')
    valid = [r for r in req.rows if to_mm(r.l) > 0 and to_mm(r.w) > 0 and to_mm(r.h) > 0 and r.n > 0]
    if not valid:
        raise HTTPException(400, '货物清单为空或没有有效行，请至少填写一行有效货物。')
    pieces = sum(r.n for r in valid)
    if pieces > MAX_PIECES:
        raise HTTPException(400, f'货物总件数 {pieces} 超过单次计算上限 {MAX_PIECES} 件。')
    params = {
        'container': list(req.container),
        'rows': [r.model_dump() for r in req.rows],
        'ship_no': req.ship_no,
        'mode': req.mode,
        'budget': min(req.budget, MAX_BUDGET),
        'bottom_metric': req.bottom_metric,
        'threshold': req.threshold,
    }
    client = request.client.host if request.client else 'unknown'
    try:
        job = jobs.submit(params, client)
    except JobRejected as exc:
        raise HTTPException(429, str(exc))
    return jobs.snapshot(job)


@app.get('/api/jobs/{job_id}')
async def job_status(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, '计算任务不存在或结果已过期，请重新计算。')
    return jobs.snapshot(job)


@app.post('/api/jobs/{job_id}/cancel')
async def cancel_job(job_id: str):
    job = jobs.cancel(job_id)
    if job is None:
        raise HTTPException(404, '计算任务不存在或结果已过期。')
    return jobs.snapshot(job)


@app.post('/api/cargo/import')
async def import_cargo(file: Optional[UploadFile] = File(None), text: Optional[str] = Form(None)):
    no_flip = priority = None
    try:
        if file is not None and file.filename:
            content = await _read_upload(file)
            name = file.filename.lower()
            if name.endswith('.txt'):
                container, boxes, ship_no, descriptions = parse_txt(_decode_text(content))
            elif name.endswith('.xlsx'):
                (container, boxes, ship_no, descriptions,
                 no_flip, priority) = parse_xlsx(io.BytesIO(content))
            else:
                raise HTTPException(400, '仅支持 txt 或 xlsx 文件')
        elif text and text.strip():
            container, boxes, ship_no, descriptions = parse_txt(text)
        else:
            raise HTTPException(400, '请先选择文件或粘贴文本。')
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 —— 解析错误原样告知用户
        raise HTTPException(400, f'导入失败：{exc}')
    return {
        'container': [c / MM for c in container],
        'ship_no': ship_no,
        'rows': _rows_from_boxes(boxes, descriptions, no_flip, priority),
    }


@app.post('/api/cargo/export')
async def export_cargo(data: CargoList, fmt: Literal['txt', 'xlsx'] = 'txt'):
    container = tuple(to_mm(v) for v in data.container)
    rows = [r.model_dump() for r in data.rows]
    if fmt == 'txt':
        return _download(build_cargo_txt(container, rows, data.ship_no),
                         '货物清单.txt', 'text/plain; charset=utf-8')
    return _download(build_cargo_xlsx(container, rows, data.ship_no), '货物清单.xlsx',
                     'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.post('/api/plan/import')
async def import_plan(file: UploadFile = File(...)):
    content = await _read_upload(file)
    try:
        packer, ship_no = load_saved_plan(file.filename or '', content)
        plan_json = json.loads(build_json_bytes(packer, ship_no))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f'方案文件无法解析：{exc}')
    form = saved_plan_form_data(packer, ship_no)
    stats = _plan_stats(packer)
    return {
        'container': [c / MM for c in form['container']],
        'ship_no': form['ship_no'],
        'rows': _rows_from_boxes(form['boxes'], form['descriptions'],
                                 form['no_flip'], form['priority']),
        'result': {'plan': plan_json, 'stats': stats,
                   'fits': stats['placed'] == stats['total'],
                   'msg': '', 'note': None, 'mode': 'imported'},
    }


@app.post('/api/plan/xlsx')
async def export_plan_xlsx(data: PlanExport):
    try:
        if len(data.plan.get('packing_plan', [])) > MAX_PIECES:
            raise ValueError('方案件数超过上限')
        packer = load_plan_into_packer(data.plan)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f'方案数据无效：{exc}')
    content = build_excel_bytes(packer, _plan_stats(packer, data.stats), data.ship_no)
    return _download(content, 'packing_plan.xlsx',
                     'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.exception_handler(HTTPException)
async def http_error(_request, exc):
    return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(_request, exc):
    errors = exc.errors()
    first = errors[0] if errors else {}
    where = ' → '.join(str(p) for p in first.get('loc', ())[1:])
    return JSONResponse({'detail': f"输入数据有误（{where}）：{first.get('msg', '')}"}, status_code=422)


# 静态页面放在最后挂载，避免覆盖 /api 路由
app.mount('/', StaticFiles(directory=WEB_DIR, html=True), name='web')
