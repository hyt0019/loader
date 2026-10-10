# -*- coding: utf-8 -*-
"""计算任务调度：每个任务在独立子进程中运行，可排队、可取消、可超时回收。

- 同时运行的任务数受 ``max_running`` 限制（默认等于 CPU 核数），其余排队；
- 浏览器每秒轮询一次任务状态；长时间没有轮询（页面已关闭）的任务会被自动取消，
  避免无人等待的计算继续占用 CPU；
- 子进程卡死或超过硬性时限时直接结束进程。
"""
import asyncio
import multiprocessing
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from server import worker

ACTIVE = ('queued', 'running')


def _mp_context():
    """Linux 上用 forkserver（预加载算法模块，新任务启动快）；Windows 开发环境用 spawn。"""
    if 'forkserver' in multiprocessing.get_all_start_methods():
        ctx = multiprocessing.get_context('forkserver')
        ctx.set_forkserver_preload(['server.worker'])
        return ctx
    return multiprocessing.get_context('spawn')


class JobRejected(Exception):
    """队列已满或同一访客的任务过多。"""


@dataclass
class Job:
    params: dict
    client: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = 'queued'
    created: float = field(default_factory=time.time)
    started: float = None
    finished: float = None
    last_seen: float = field(default_factory=time.time)
    deadline: float = None
    result: dict = None
    error: str = None
    process: object = None
    conn: object = None


class JobManager:
    def __init__(self, max_running=2, max_queued=20, max_per_client=2,
                 abandon_after=150.0, keep_finished=600.0, extra_seconds=600.0):
        self.max_running = max_running
        self.max_queued = max_queued
        self.max_per_client = max_per_client
        # 浏览器后台标签页的定时器可能被节流到每分钟一次，因此放弃判定留足余量
        self.abandon_after = abandon_after
        self.keep_finished = keep_finished
        # 硬性时限 = 搜索时间上限（增强版）+ 该余量；标准版只有余量
        self.extra_seconds = extra_seconds
        self.jobs = {}
        self.queue = deque()
        self._ctx = _mp_context()

    # ---------------- 对外接口（均在事件循环线程中调用） ---------------- #
    def submit(self, params, client):
        active = [j for j in self.jobs.values() if j.status in ACTIVE]
        if sum(1 for j in active if j.client == client) >= self.max_per_client:
            raise JobRejected('您已有计算正在进行，请等待完成或取消后再试。')
        if len(self.queue) >= self.max_queued:
            raise JobRejected('服务器繁忙，排队人数已满，请稍后再试。')
        job = Job(params=params, client=client)
        self.jobs[job.id] = job
        self.queue.append(job.id)
        self._start_queued()
        return job

    def get(self, job_id):
        job = self.jobs.get(job_id)
        if job is not None:
            job.last_seen = time.time()
        return job

    def cancel(self, job_id):
        job = self.jobs.get(job_id)
        if job is None or job.status not in ACTIVE:
            return job
        self._finish(job, 'cancelled', error='已取消')
        return job

    def snapshot(self, job):
        now = time.time()
        data = {'id': job.id, 'status': job.status,
                'mode': job.params['mode'], 'budget': job.params['budget']}
        if job.status == 'queued':
            data['position'] = list(self.queue).index(job.id) + 1 if job.id in self.queue else 0
        if job.started:
            data['elapsed'] = round((job.finished or now) - job.started, 1)
        if job.status == 'done':
            data['result'] = job.result
        if job.error:
            data['error'] = job.error
        return data

    async def supervise(self, interval=0.25):
        """后台循环：收取结果、处理超时 / 放弃的任务、启动排队任务、清理旧结果。"""
        while True:
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 —— 调度循环不能因单个任务出错而停止
                print(f'[jobs] supervise error: {exc!r}', flush=True)
            await asyncio.sleep(interval)

    def shutdown(self):
        for job in list(self.jobs.values()):
            if job.status in ACTIVE:
                self._finish(job, 'cancelled', error='服务已停止')

    # ---------------- 内部 ---------------- #
    def _tick(self):
        now = time.time()
        for job in list(self.jobs.values()):
            if job.status == 'running':
                self._collect(job, now)
            if job.status in ACTIVE and now - job.last_seen > self.abandon_after:
                self._finish(job, 'cancelled', error='页面已关闭，计算已自动停止')
        self._start_queued()
        for job in list(self.jobs.values()):
            if job.finished and now - job.finished > self.keep_finished:
                del self.jobs[job.id]

    def _collect(self, job, now):
        if job.conn.poll():
            try:
                kind, payload = job.conn.recv()
            except (EOFError, OSError):
                kind, payload = 'error', '计算进程意外退出'
            if kind == 'done':
                job.result = payload
                self._finish(job, 'done')
            else:
                self._finish(job, 'error', error=payload)
        elif not job.process.is_alive():
            self._finish(job, 'error', error='计算进程意外退出（可能是服务器内存不足）')
        elif now > job.deadline:
            self._finish(job, 'error', error='计算超时，已自动停止。可减少货物种类或缩短搜索时间后重试。')

    def _start_queued(self):
        running = sum(1 for j in self.jobs.values() if j.status == 'running')
        while self.queue and running < self.max_running:
            job = self.jobs.get(self.queue.popleft())
            if job is None or job.status != 'queued':
                continue
            parent_conn, child_conn = self._ctx.Pipe(duplex=False)
            process = self._ctx.Process(target=worker.run_job, args=(job.params, child_conn),
                                        daemon=True)
            process.start()
            child_conn.close()  # 主进程只保留读端，子进程退出时读端才能感知 EOF
            job.process, job.conn = process, parent_conn
            job.status, job.started = 'running', time.time()
            budget = job.params['budget'] if job.params['mode'] == 'enhanced' else 0
            job.deadline = job.started + budget + self.extra_seconds
            running += 1

    def _finish(self, job, status, error=None):
        if job.id in self.queue:
            self.queue.remove(job.id)
        if job.process is not None:
            if job.process.is_alive():
                job.process.terminate()
            job.process.join(timeout=2)
            if job.process.is_alive():
                job.process.kill()
                job.process.join(timeout=1)
            job.process = None
        if job.conn is not None:
            job.conn.close()
            job.conn = None
        job.status, job.error = status, error
        job.finished = time.time()
