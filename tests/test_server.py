"""网站后端（server/）接口回归测试。

需要安装 requirements-server.txt 与 httpx；未安装时自动跳过。
运行：python -m unittest discover -s tests
"""
import importlib.util
import io
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HAS_DEPS = all(importlib.util.find_spec(name) for name in ('fastapi', 'httpx', 'python_multipart'))

ROW = {'l': 1.0, 'w': 1.0, 'h': 1.0, 'n': 8, 'wt': 10.0, 'type': '纸箱',
       'no_flip': False, 'prio': 0, 'desc': '立方件'}
JOB = {'container': [2.0, 2.0, 2.0], 'rows': [ROW], 'ship_no': 'SO-T1',
       'mode': 'standard', 'budget': 60, 'bottom_metric': 'none', 'threshold': 100}


@unittest.skipUnless(HAS_DEPS, '需要 fastapi / httpx / python-multipart')
class ServerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from server import main
        cls.main = main
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def wait(self, job_id, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = self.client.get(f'/api/jobs/{job_id}').json()
            if snap['status'] not in ('queued', 'running'):
                return snap
            time.sleep(0.2)
        self.fail('计算任务超时')

    def test_static_page_and_headers(self):
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('HYT', page.text)
        self.assertEqual(page.headers['cache-control'], 'no-cache')
        self.assertIn("script-src 'self'", page.headers['content-security-policy'])
        vendor = self.client.get('/vendor/three-0.186.1.bundle.min.js')
        self.assertEqual(vendor.status_code, 200)
        self.assertIn('immutable', vendor.headers['cache-control'])
        self.assertEqual(self.client.get('/api/health').json(), {'status': 'ok'})

    def test_job_returns_plan_in_saved_plan_format(self):
        snap = self.client.post('/api/jobs', json=JOB)
        self.assertEqual(snap.status_code, 202)
        done = self.wait(snap.json()['id'])
        self.assertEqual(done['status'], 'done', done.get('error'))
        result = done['result']
        self.assertTrue(result['fits'])
        self.assertEqual(result['stats']['placed'], 8)
        plan = result['plan']
        self.assertEqual(plan['ship_no'], 'SO-T1')
        self.assertEqual(plan['container'], [2.0, 2.0, 2.0])
        self.assertEqual(len(plan['packing_plan']), 8)
        self.assertEqual(plan['boxes'][0][6], '立方件')

    def test_job_with_stacking_rules(self):
        rows = [{**ROW, 'n': 4, 'wt': 10.0, 'desc': '轻'},
                {**ROW, 'l': 0.5, 'w': 0.5, 'h': 0.5, 'n': 4, 'wt': 50.0, 'desc': '重', 'no_stack': True}]
        job = {**JOB, 'rows': rows, 'bottom_metric': 'volumetric', 'strict_stack': True}
        done = self.wait(self.client.post('/api/jobs', json=job).json()['id'])
        self.assertEqual(done['status'], 'done', done.get('error'))
        plan = done['result']['plan']
        self.assertEqual(plan['strict_stack'], 'volumetric')
        self.assertEqual([box[9] for box in plan['boxes']], [False, True])
        self.assertEqual(done['result']['stats']['placed'], 8)
        # 未选指标时严格执行不生效
        loose = self.wait(self.client.post('/api/jobs', json={**job, 'bottom_metric': 'none'}).json()['id'])
        self.assertEqual(loose['result']['plan']['strict_stack'], '')

    def test_cancel_stops_job(self):
        job_id = self.client.post('/api/jobs', json={**JOB, 'mode': 'enhanced'}).json()['id']
        snap = self.client.post(f'/api/jobs/{job_id}/cancel').json()
        self.assertEqual(snap['status'], 'cancelled')
        self.assertEqual(self.client.get(f'/api/jobs/{job_id}').json()['status'], 'cancelled')

    def test_abandoned_job_is_cancelled(self):
        jobs = self.main.jobs
        job_id = self.client.post('/api/jobs', json=JOB).json()['id']
        jobs.jobs[job_id].last_seen -= jobs.abandon_after + 1  # 模拟页面关闭后不再轮询
        jobs._tick()
        self.assertEqual(jobs.jobs[job_id].status, 'cancelled')

    def test_per_client_limit(self):
        ids = [self.client.post('/api/jobs', json={**JOB, 'mode': 'enhanced'}).json()['id']
               for _ in range(self.main.jobs.max_per_client)]
        try:
            rejected = self.client.post('/api/jobs', json=JOB)
            self.assertEqual(rejected.status_code, 429)
        finally:
            for job_id in ids:
                self.client.post(f'/api/jobs/{job_id}/cancel')

    def test_validation_messages(self):
        bad_container = self.client.post('/api/jobs', json={**JOB, 'container': [0, 2, 2]})
        self.assertEqual(bad_container.status_code, 400)
        self.assertIn('集装箱尺寸', bad_container.json()['detail'])
        empty = self.client.post('/api/jobs', json={**JOB, 'rows': [{**ROW, 'l': 0}]})
        self.assertEqual(empty.status_code, 400)
        bad_type = self.client.post('/api/jobs', json={**JOB, 'rows': [{**ROW, 'type': '铁箱'}]})
        self.assertEqual(bad_type.status_code, 422)
        self.assertIn('输入数据有误', bad_type.json()['detail'])

    def test_cargo_export_import_round_trip(self):
        rows = [ROW, {**ROW, 'l': 0.5, 'type': '托盘', 'no_flip': True, 'prio': 3, 'desc': '底层件',
                      'no_stack': True}]
        body = {'container': [5.8, 2.35, 2.35], 'ship_no': 'SO-T2', 'rows': rows}
        for fmt in ('txt', 'xlsx'):
            exported = self.client.post(f'/api/cargo/export?fmt={fmt}', json=body)
            self.assertEqual(exported.status_code, 200)
            imported = self.client.post('/api/cargo/import',
                                        files={'file': (f'list.{fmt}', exported.content)}).json()
            self.assertEqual(imported['container'], [5.8, 2.35, 2.35])
            self.assertEqual(imported['ship_no'], 'SO-T2')
            self.assertEqual([r['type'] for r in imported['rows']], ['纸箱', '托盘'])
            self.assertEqual([r['desc'] for r in imported['rows']], ['立方件', '底层件'])
            if fmt == 'xlsx':  # TXT 格式本身不保存禁止倒放、优先级与不可压
                self.assertEqual([r['no_flip'] for r in imported['rows']], [False, True])
                self.assertEqual([r['prio'] for r in imported['rows']], [0, 3])
                self.assertEqual([r['no_stack'] for r in imported['rows']], [False, True])

    def test_cargo_import_from_pasted_gbk_text(self):
        text = '5.8 2.35 2.35\n1\n1 1 1 8 10 1\n'
        pasted = self.client.post('/api/cargo/import', data={'text': text}).json()
        self.assertEqual(pasted['rows'][0]['n'], 8)
        gbk = ('# 发货单号: 中文单号\n' + text).encode('gbk')
        uploaded = self.client.post('/api/cargo/import', files={'file': ('a.txt', gbk)}).json()
        self.assertEqual(uploaded['ship_no'], '中文单号')

    def test_plan_xlsx_export_import_round_trip(self):
        done = self.wait(self.client.post('/api/jobs', json=JOB).json()['id'])
        result = done['result']
        xlsx = self.client.post('/api/plan/xlsx', json={'plan': result['plan'], 'stats': result['stats'],
                                                        'ship_no': 'SO-T3'})
        self.assertEqual(xlsx.status_code, 200)
        imported = self.client.post('/api/plan/import',
                                    files={'file': ('packing_plan.xlsx', io.BytesIO(xlsx.content))}).json()
        self.assertEqual(imported['ship_no'], 'SO-T3')
        self.assertEqual(imported['container'], [2.0, 2.0, 2.0])
        self.assertEqual(imported['rows'][0]['n'], 8)
        self.assertEqual(imported['result']['stats']['placed'], 8)
        self.assertEqual(len(imported['result']['plan']['packing_plan']), 8)

    def test_unknown_job_and_bad_plan(self):
        self.assertEqual(self.client.get('/api/jobs/does-not-exist').status_code, 404)
        bad = self.client.post('/api/plan/import', files={'file': ('x.json', b'{not json')})
        self.assertEqual(bad.status_code, 400)
        self.assertIn('方案文件无法解析', bad.json()['detail'])


if __name__ == '__main__':
    unittest.main()
