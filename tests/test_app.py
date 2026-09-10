"""Flask 路由端到端测试（使用内存式临时数据库隔离）。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
db.DB_PATH = Path(_tmp.name)
db.init_db()

import app as appmod  # noqa: E402


def plan_config():
    return {
        "plate_type": 96,
        "stock_concentration": 100,
        "pipette_ranges": [{"min": 2, "max": 200}],
        "min_mix": 50,
        "dead_volume": 20,
        "targets": [
            {"concentration": 10, "volume": 100, "replicates": 2},
            {"concentration": 1, "volume": 100, "replicates": 2},
        ],
        "blank_count": 1,
        "wells": [],
    }


class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = appmod.app.test_client()

    def test_index_and_assets(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)
        self.assertEqual(self.client.get("/static/style.css").status_code, 200)

    def test_examples(self):
        r = self.client.get("/api/examples")
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(len(r.get_json()), 3)

    def test_plan_ok_and_bad(self):
        r = self.client.post("/api/plan", json={"config": plan_config()})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["feasible"])
        self.assertGreater(len(data["steps"]), 0)
        r = self.client.post("/api/plan", json={"config": {"stock_concentration": 0}})
        self.assertEqual(r.status_code, 400)

    def test_experiment_lifecycle(self):
        r = self.client.post("/api/experiments",
                             json={"name": "接口生命周期", "config": plan_config()})
        self.assertEqual(r.status_code, 201)
        eid = r.get_json()["id"]

        r = self.client.get(f"/api/experiments/{eid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("step_states", r.get_json())

        n_steps = len(r.get_json()["result"]["steps"])
        # 勾选步骤
        r = self.client.post(f"/api/experiments/{eid}/steps/1", json={"done": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["done"], 1)
        # 取消
        r = self.client.post(f"/api/experiments/{eid}/steps/1", json={"done": False})
        self.assertEqual(r.get_json()["done"], 0)
        # 重置
        self.client.post(f"/api/experiments/{eid}/steps/2", json={"done": True})
        self.client.post(f"/api/experiments/{eid}/reset", json={})
        exp = self.client.get(f"/api/experiments/{eid}").get_json()
        self.assertFalse(any(s["done"] for s in exp["step_states"].values()))
        self.assertTrue(any(l["action"] == "reset" for l in exp["logs"]))

        # 重算（PUT）后实验仍可获取
        cfg = plan_config()
        cfg["targets"] = [{"concentration": 10, "volume": 100, "replicates": 1}]
        r = self.client.put(f"/api/experiments/{eid}",
                            json={"name": "改名", "config": cfg})
        self.assertEqual(r.status_code, 200)

        # CSV
        r = self.client.get(f"/api/experiments/{eid}/export.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("孔位明细", r.data.decode("utf-8-sig"))
        # 工作单
        r = self.client.get(f"/api/experiments/{eid}/worksheet")
        self.assertEqual(r.status_code, 200)
        self.assertIn("台面操作步骤", r.data.decode("utf-8"))

        # 列表 + 删除
        ids = [e["id"] for e in self.client.get("/api/experiments").get_json()]
        self.assertIn(eid, ids)
        self.assertEqual(self.client.delete(f"/api/experiments/{eid}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/experiments/{eid}").status_code, 404)

    def test_adhoc_exports(self):
        r = self.client.post("/api/export.csv", json={"config": plan_config()})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.decode("utf-8-sig").startswith("# 96"))
        r = self.client.post("/api/worksheet", json={"config": plan_config()})
        self.assertEqual(r.status_code, 200)
        self.assertIn("板图", r.data.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
