"""规划引擎本地测试：物料守恒、来源链、越界/容量/不可达/循环/锁定重算。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest

import planner


def base_config(**overrides):
    cfg = {
        "plate_type": 96,
        "stock_concentration": 100,
        "pipette_ranges": [{"min": 2, "max": 20}, {"min": 20, "max": 200}],
        "min_mix": 50,
        "dead_volume": 20,
        "max_chain": 12,
        "blank_count": 0,
        "targets": [
            {"concentration": 10, "volume": 100, "replicates": 2},
            {"concentration": 1, "volume": 100, "replicates": 2},
        ],
        "wells": [],
    }
    cfg.update(overrides)
    return cfg


def codes(result, severity=None):
    return {i["code"] for i in result["issues"]
            if severity is None or i["severity"] == severity}


class TestSerialDilution(unittest.TestCase):
    def test_material_balance(self):
        res = planner.plan(base_config())
        self.assertTrue(res["feasible"], res["issues"])
        for w in res["wells"]:
            a = w["analyte_transfer"]["volume"]
            d = w["diluent_transfer"]["volume"] if w["diluent_transfer"] else 0
            # 孔内总进液 = 配制体积（终体积 + 供出 + 死体积）
            self.assertAlmostEqual(a + d, w["prep_volume"], places=3)
            self.assertGreaterEqual(w["prep_volume"], w["final_volume"] - 1e-6)
            src = w["source_liquid_conc"]
            # 终浓度守恒（保留部分）：来源液浓度·Va / Vfinal = Ctarget
            actual = src * a / w["final_volume"]
            self.assertAlmostEqual(actual, w["target_conc"], delta=w["target_conc"] * 1e-6 + 1e-9)
            self.assertAlmostEqual(w["computed_conc"], w["target_conc"],
                                   delta=w["target_conc"] * 1e-6 + 1e-9)
            self.assertLessEqual(a, w["final_volume"] + 1e-6)

    def test_sources_chain_same_replicate(self):
        res = planner.plan(base_config())
        labels = {w["label"]: w for w in res["wells"]}
        # 第一级来自母液
        level0 = [w for w in res["wells"] if w["level"] == 0]
        self.assertTrue(all(w["source"] == "stock" for w in level0))
        # 第二级来自同平行样的上一级
        level1 = [w for w in res["wells"] if w["level"] == 1]
        for w in level1:
            self.assertNotEqual(w["source"], "stock")
            parent = labels[planner.slot_label(int(w["source"]), res["cols"])]
            self.assertEqual(parent["replicate"], w["replicate"])
            self.assertEqual(parent["level"], 0)

    def test_tenfold_transfer_volume(self):
        # 1:10 稀释 100µL -> 取 10µL 母液；保留部分稀释液 90µL
        # w0 还向下游供出 10µL + 死体积 20µL，故总稀释液 = 90+10+20
        res = planner.plan(base_config())
        w0 = [w for w in res["wells"] if w["level"] == 0][0]
        self.assertAlmostEqual(w0["analyte_transfer"]["volume"], 10, places=3)
        self.assertAlmostEqual(w0["prep_volume"], 130, places=3)
        self.assertAlmostEqual(w0["diluent_transfer"]["volume"], 120, places=3)
        w1 = [w for w in res["wells"] if w["level"] == 1][0]
        self.assertAlmostEqual(w1["analyte_transfer"]["volume"], 10, places=3)
        self.assertAlmostEqual(w1["diluent_transfer"]["volume"], 90, places=3)


class TestFeasibilityChecks(unittest.TestCase):
    def test_concentration_unreachable(self):
        cfg = base_config(stock_concentration=5, targets=[
            {"concentration": 10, "volume": 100, "replicates": 1}])
        res = planner.plan(cfg)
        self.assertIn("CONC_UNREACHABLE", codes(res))
        self.assertFalse(res["feasible"])
        w = res["wells"][0]
        self.assertTrue(w["deviations"])
        self.assertTrue(any(d["kind"] == "concentration" for d in w["deviations"]))

    def test_volume_below_min_range_repaired(self):
        # 单一浓度点直连母液：100µM -> 0.01µM @100µL 理论取液 0.01µL < 2µL
        # 无中间孔可搭桥，最接近方案提升到最小量程并报偏差
        cfg = base_config(max_chain=1, targets=[
            {"concentration": 0.01, "volume": 100, "replicates": 1}])
        res = planner.plan(cfg)
        self.assertIn("VOLUME_RANGE", codes(res))
        w = res["wells"][0]
        self.assertGreaterEqual(w["analyte_transfer"]["volume"], 2 - 1e-6)

    def test_long_chain_bridges_tiny_transfer(self):
        # 提供 1:10 中间浓度点时，小转移量可经级数桥接，方案可行
        cfg = base_config(targets=[
            {"concentration": 10, "volume": 100, "replicates": 1},
            {"concentration": 1, "volume": 100, "replicates": 1},
            {"concentration": 0.1, "volume": 100, "replicates": 1},
        ])
        res = planner.plan(cfg)
        self.assertTrue(res["feasible"], res["issues"])

    def test_capacity_exceeded(self):
        cfg = base_config(well_capacity=280, targets=[
            {"concentration": 10, "volume": 400, "replicates": 1}])
        res = planner.plan(cfg)
        self.assertIn("CAPACITY", codes(res))

    def test_plate_full(self):
        cfg = base_config(plate_type=24, blank_count=30, targets=[
            {"concentration": 10, "volume": 500, "replicates": 1}])
        res = planner.plan(cfg)
        self.assertIn("PLATE_FULL", codes(res))
        self.assertLessEqual(len(res["wells"]), 24)

    def test_source_cycle_detected(self):
        cfg = base_config(wells=[
            {"slot": 0, "role": "sample", "level": 0, "replicate": 1,
             "locked": True, "source": "1"},
            {"slot": 1, "role": "sample", "level": 1, "replicate": 1,
             "locked": True, "source": "0"},
        ])
        res = planner.plan(cfg)
        self.assertIn("SOURCE_CYCLE", codes(res))
        # 退回母液后仍能给出可执行（最接近）方案
        for w in res["wells"]:
            self.assertIsNotNone(w["computed_conc"])

    def test_invalid_source(self):
        cfg = base_config(wells=[
            {"slot": 0, "role": "sample", "level": 0, "replicate": 1,
             "locked": True, "source": "999"},
        ])
        res = planner.plan(cfg)
        self.assertIn("SOURCE_INVALID", codes(res))

    def test_step_depth_warning(self):
        cfg = base_config(max_chain=1, targets=[
            {"concentration": 10, "volume": 100, "replicates": 1},
            {"concentration": 1, "volume": 100, "replicates": 1},
        ])
        res = planner.plan(cfg)
        self.assertIn("STEP_DEPTH", codes(res))

    def test_stock_short_warning(self):
        cfg = base_config(stock_volume=5, targets=[
            {"concentration": 10, "volume": 100, "replicates": 2}])
        res = planner.plan(cfg)
        self.assertIn("STOCK_SHORT", codes(res, severity="warning"))


class TestLockedReroute(unittest.TestCase):
    def test_locked_well_preserved_and_others_fill(self):
        cfg = base_config(wells=[
            {"slot": 10, "role": "sample", "level": 0, "replicate": 1, "locked": True},
        ])
        res = planner.plan(cfg)
        w10 = next(w for w in res["wells"] if w["slot"] == 10)
        self.assertTrue(w10["locked"])
        self.assertEqual(w10["level"], 0)
        self.assertEqual(len(res["wells"]), 4)

    def test_drag_swap_explicit_board(self):
        # 模拟前端拖拽后回传完整布板
        cfg = base_config(wells=[
            {"slot": 5, "role": "sample", "level": 0, "replicate": 1, "locked": True},
            {"slot": 6, "role": "sample", "level": 0, "replicate": 2, "locked": False},
            {"slot": 7, "role": "sample", "level": 1, "replicate": 1, "locked": False},
            {"slot": 8, "role": "sample", "level": 1, "replicate": 2, "locked": False},
        ])
        res = planner.plan(cfg)
        slots = {w["slot"]: (w["level"], w["replicate"]) for w in res["wells"]}
        self.assertEqual(slots[5], (0, 1))
        self.assertEqual(slots[6], (0, 2))
        self.assertEqual(slots[7], (1, 1))
        self.assertEqual(slots[8], (1, 2))


    def test_explicit_board_unplaced_demand(self):
        # 布板只放了 1 个孔，但有 4 个需求 -> 其余自动补孔并报 PLATE_FULL?（空孔足够时不报）
        cfg = base_config(wells=[
            {"slot": 20, "role": "sample", "level": 0, "replicate": 1, "locked": True},
        ])
        res = planner.plan(cfg)
        self.assertTrue(res["feasible"], res["issues"])
        self.assertEqual(len(res["wells"]), 4)
        self.assertTrue(next(w for w in res["wells"] if w["slot"] == 20)["locked"])

        # 板被占满仍有需求未放置
        full = [{"slot": s, "role": "sample", "level": 0, "replicate": 1,
                 "concentration": 10, "volume": 100, "locked": True}
                for s in range(24)]
        cfg2 = base_config(plate_type=24, wells=full,
                           targets=[{"concentration": 10, "volume": 500, "replicates": 30}])
        res2 = planner.plan(cfg2)
        self.assertIn("PLATE_FULL", codes(res2))


class TestBlanksAnd24Plate(unittest.TestCase):
    def test_blanks_diluent_only(self):
        cfg = base_config(plate_type=24, stock_concentration=2000,
                          blank_count=2, blank_volume=500,
                          pipette_ranges=[{"min": 20, "max": 1000}],
                          targets=[{"concentration": 1000, "volume": 500, "replicates": 1},
                                   {"concentration": 500, "volume": 500, "replicates": 1}])
        res = planner.plan(cfg)
        self.assertTrue(res["feasible"], res["issues"])
        blanks = [w for w in res["wells"] if w["role"] == "blank"]
        self.assertEqual(len(blanks), 2)
        for w in blanks:
            self.assertIsNone(w["analyte_transfer"])
            self.assertAlmostEqual(w["diluent_transfer"]["volume"], 500, places=3)

    def test_steps_include_all_transfers_and_ordered(self):
        res = planner.plan(base_config(blank_count=1))
        orders = [s["order"] for s in res["steps"]]
        self.assertEqual(orders, list(range(1, len(orders) + 1)))
        # 每步文本含 µL
        for s in res["steps"]:
            self.assertIn("µL", s["text"])


class TestPipetteSplit(unittest.TestCase):
    def test_large_volume_split(self):
        feasible, chunks, note = planner.pipette_check(300, [{"min": 2, "max": 20},
                                                             {"min": 20, "max": 200}])
        self.assertTrue(feasible)
        self.assertEqual(len(chunks), 2)
        self.assertAlmostEqual(sum(chunks), 300, places=3)

    def test_tiny_volume_infeasible(self):
        feasible, chunks, note = planner.pipette_check(0.5, [{"min": 2, "max": 20}])
        self.assertFalse(feasible)


class TestValidation(unittest.TestCase):
    def test_bad_stock(self):
        with self.assertRaises(ValueError):
            planner.plan(base_config(stock_concentration=0))

    def test_no_targets(self):
        with self.assertRaises(ValueError):
            planner.plan(base_config(targets=[]))

    def test_bad_plate(self):
        with self.assertRaises(ValueError):
            planner.plan(base_config(plate_type=48))


if __name__ == "__main__":
    unittest.main()
