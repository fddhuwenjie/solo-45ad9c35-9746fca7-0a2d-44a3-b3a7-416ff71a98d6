"""微孔板梯度稀释规划引擎。

依据物料守恒（C_src * V_transfer = C_target * V_final）为 24/96 孔板生成
可复现的连续稀释 / 直接稀释方案，并完成全部本地可行性校验：

- 移液器量程越界（支持多档移液器、分次移液）
- 孔容量不足
- 浓度不可达（母液浓度不足 / 超过最大连续稀释级数）
- 来源循环 / 无效来源
- 孔位冲突 / 孔位不足
- 最小混匀体积与死体积约束

当严格目标不可行时，求解器会迭代修复来源链并给出“最接近可行方案”，
每个孔携带 deviations（目标值 vs 可实现值）。
"""

from __future__ import annotations

import math
from typing import Any

# 板型：(行数, 列数, 默认单孔容量 µL)
PLATES = {
    24: {"rows": 4, "cols": 6, "default_capacity": 2900.0},
    96: {"rows": 8, "cols": 12, "default_capacity": 280.0},
}

MAX_REPAIR_ROUNDS = 10
STOCK = "stock"
DILUENT = "diluent"

ERROR = "error"
WARNING = "warning"

ISSUE_MESSAGES = {
    "PLATE_FULL": "孔位不足",
    "WELL_CONFLICT": "孔位冲突",
    "CAPACITY": "孔容量不足",
    "VOLUME_RANGE": "移液量越界",
    "CONC_UNREACHABLE": "浓度不可达",
    "SOURCE_CYCLE": "来源循环",
    "SOURCE_INVALID": "无效来源",
    "STEP_DEPTH": "超过允许的连续稀释级数",
    "MIN_MIX": "低于最小混匀体积",
    "DEAD_VOLUME": "死体积不足",
    "STOCK_SHORT": "母液总量不足",
}


def row_col_of(slot: int, cols: int) -> tuple[int, int]:
    div, rem = divmod(slot, cols)
    return div + 1, rem + 1


def slot_label(slot: int, cols: int) -> str:
    r, c = row_col_of(slot, cols)
    return f"{chr(64 + r)}{c}"


def _num(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def normalize_config(raw: dict | None) -> dict:
    """补全并校验前端输入，返回规范化配置。非法基础输入抛 ValueError。"""
    raw = raw or {}
    plate_type = _int(raw.get("plate_type"), 96)
    if plate_type not in PLATES:
        raise ValueError("板型仅支持 24 或 96")
    spec = PLATES[plate_type]

    stock_conc = _num(raw.get("stock_concentration"), 0.0)
    if stock_conc <= 0:
        raise ValueError("母液浓度必须为正数")

    ranges: list[dict] = []
    ranges_raw = raw.get("pipette_ranges")
    if isinstance(ranges_raw, list) and ranges_raw:
        for rng in ranges_raw:
            lo, hi = _num(rng.get("min"), 0.0), _num(rng.get("max"), 0.0)
            if lo > 0 and hi >= lo:
                ranges.append({"min": lo, "max": hi})
    if not ranges:
        pmin = _num(raw.get("pipette_min"), 2.0)
        pmax = _num(raw.get("pipette_max"), 200.0)
        if pmin <= 0 or pmax < pmin:
            raise ValueError("移液器量程无效")
        ranges = [{"min": pmin, "max": pmax}]
    ranges.sort(key=lambda r: r["max"])

    targets = []
    for t in raw.get("targets", []) or []:
        conc = _num(t.get("concentration"), 0.0)
        vol = _num(t.get("volume"), 0.0)
        reps = max(1, _int(t.get("replicates"), 1))
        if conc > 0 and vol > 0:
            targets.append({"concentration": conc, "volume": vol, "replicates": reps})
    if not targets:
        raise ValueError("至少需要一个有效目标浓度")

    blank_count = max(0, _int(raw.get("blank_count"), 0))
    blank_vol = _num(raw.get("blank_volume"), targets[0]["volume"])
    if blank_vol <= 0:
        blank_vol = targets[0]["volume"]

    fixed_wells = []
    seen: set[int] = set()
    for w in raw.get("wells", []) or []:
        slot = _int(w.get("slot"), -1)
        if slot < 0 or slot >= spec["rows"] * spec["cols"] or slot in seen:
            continue
        seen.add(slot)
        source = w.get("source")
        entry = {
            "slot": slot,
            "locked": bool(w.get("locked")),
            "source": str(source) if source not in (None, "") else None,
            # 拖拽后前端会回传完整布板；以下字段可选
            "role": w.get("role"),
            "level": w.get("level"),
            "replicate": w.get("replicate"),
            "concentration": w.get("concentration"),
            "volume": w.get("volume"),
        }
        fixed_wells.append(entry)

    return {
        "plate_type": plate_type,
        "rows": spec["rows"],
        "cols": spec["cols"],
        "capacity": _num(raw.get("well_capacity"), spec["default_capacity"]),
        "stock_concentration": stock_conc,
        "stock_volume": _num(raw.get("stock_volume"), 0.0) or None,
        "concentration_unit": str(raw.get("concentration_unit") or "µM"),
        "pipette_ranges": ranges,
        "pipette_min": ranges[0]["min"],
        "min_mix": _num(raw.get("min_mix"), 50.0),
        "dead_volume": _num(raw.get("dead_volume"), 10.0),
        "max_chain": max(1, _int(raw.get("max_chain"), 12)),
        "targets": sorted(targets, key=lambda t: -t["concentration"]),
        "blank_count": blank_count,
        "blank_volume": blank_vol,
        "wells": fixed_wells,
    }


# --------------------------------------------------------------------------- #
# 移液器可行性
# --------------------------------------------------------------------------- #

def pipette_check(volume: float, ranges: list[dict]) -> tuple[bool, list[float], str]:
    """返回 (是否严格可行, 分次体积列表, 说明)。

    优先单次移液；否则在某一档量程内分次，每片均在量程内。
    无法严格满足时给出最接近的分次方案（可能有一片越界，调用方据此报偏差）。
    """
    if volume <= 0:
        return True, [], "无需移液"
    for rng in ranges:
        if rng["min"] <= volume <= rng["max"]:
            return True, [round(volume, 3)], "单次移液"
    # 分次：在各档中选“片数最少且每片均在量程内”的方案
    best_split: tuple[int, list[float], str] | None = None
    for rng in ranges:
        k = max(1, math.ceil(volume / rng["max"]))
        if k * rng["min"] <= volume:
            candidate = (k, [round(volume / k, 3)] * k,
                         f"分 {k} 次移液（{_fmt(rng['min'])}–{_fmt(rng['max'])} µL）")
            if best_split is None or k < best_split[0]:
                best_split = candidate
    if best_split is not None:
        return True, best_split[1], best_split[2]
    # 最接近方案：用最大量程档位，k-1 片满量程 + 末片补差
    rng = max(ranges, key=lambda r: r["max"])
    k = max(1, math.ceil(volume / rng["max"]))
    full = min(rng["max"], volume)
    chunks = [round(full, 3)] * (k - 1) + [round(volume - full * (k - 1), 3)]
    return False, chunks, f"最接近分次（量程 {_fmt(rng['min'])}–{_fmt(rng['max'])} µL）"


def _fmt(x: float) -> str:
    return f"{x:g}"


# --------------------------------------------------------------------------- #
# 主规划
# --------------------------------------------------------------------------- #

def plan(raw_config: dict | None) -> dict:
    cfg = normalize_config(raw_config)
    rows, cols = cfg["rows"], cfg["cols"]
    n_slots = rows * cols
    issues: list[dict] = []

    def add_issue(code: str, slots=None, message=None, severity=ERROR):
        issues.append({
            "code": code,
            "severity": severity,
            "slots": slots or [],
            "message": message or ISSUE_MESSAGES[code],
        })

    # 1) 需求清单（目标浓度自高到低，每个目标展开平行样）---------------------
    demands: list[dict] = []
    for level, t in enumerate(cfg["targets"]):
        for rep in range(1, t["replicates"] + 1):
            demands.append({
                "role": "sample",
                "level": level,
                "replicate": rep,
                "target_conc": t["concentration"],
                "final_volume": t["volume"],
            })
    for _ in range(cfg["blank_count"]):
        demands.append({
            "role": "blank",
            "level": -1,
            "replicate": 0,
            "target_conc": 0.0,
            "final_volume": cfg["blank_volume"],
        })

    fixed_by_slot: dict[int, dict] = {}
    for w in cfg["wells"]:
        fixed_by_slot.setdefault(w["slot"], w)
    if len(fixed_by_slot) != len(cfg["wells"]):
        add_issue("WELL_CONFLICT", message="存在重复孔位定义，已忽略重复项")

    explicit_fixed = any(fw.get("role") in ("sample", "blank")
                         for fw in fixed_by_slot.values())
    if not explicit_fixed:
        total_need = len(demands) + len(fixed_by_slot)
        if total_need > n_slots:
            add_issue(
                "PLATE_FULL",
                message=f"共需 {total_need} 孔，{cfg['plate_type']} 孔板仅 {n_slots} 孔，"
                        f"超出 {total_need - n_slots} 孔；已截断超出部分",
            )

    # 2) 孔位分配 -------------------------------------------------------------
    wells: dict[int, dict] = {}

    def demand_from_fixed(fw: dict) -> dict:
        """回传布板：显式 role/level 优先；否则按目标列表顺序取需求。"""
        role = fw.get("role")
        if role in ("sample", "blank"):
            level = _int(fw.get("level"), 0)
            if role == "blank" or level < 0:
                return {"role": "blank", "level": -1, "replicate": 0,
                        "target_conc": 0.0,
                        "final_volume": _num(fw.get("volume"), cfg["blank_volume"])}
            t = (cfg["targets"][level] if 0 <= level < len(cfg["targets"])
                 else cfg["targets"][0])
            return {
                "role": "sample",
                "level": level,
                "replicate": max(1, _int(fw.get("replicate"), 1)),
                "target_conc": _num(fw.get("concentration"), t["concentration"]),
                "final_volume": _num(fw.get("volume"), t["volume"]),
            }
        d = take_demand()
        if d is None:
            t0 = cfg["targets"][0]
            d = {"role": "sample", "level": 0, "replicate": 1,
                 "target_conc": t0["concentration"], "final_volume": t0["volume"]}
        return d

    used_demand = [False] * len(demands)

    def take_demand():
        for i, d in enumerate(demands):
            if not used_demand[i]:
                used_demand[i] = True
                return d
        return None

    explicit_fixed_board = explicit_fixed
    for slot in sorted(fixed_by_slot):
        fw = fixed_by_slot[slot]
        d = demand_from_fixed(fw)
        wells[slot] = _make_well(slot, cols, d, fw)
        if explicit_fixed_board and fw.get("role") in ("sample", "blank"):
            # 标记对应需求已被固定孔占用（按 角色/级别/平行样 匹配）
            for i, dd in enumerate(demands):
                if not used_demand[i] and dd["role"] == d["role"] \
                        and (dd["role"] == "blank"
                             or (dd["level"] == d["level"]
                                 and dd["replicate"] == d["replicate"])):
                    used_demand[i] = True
                    break

    # 空槽位按需求顺序继续填充
    for slot in range(n_slots):
        if slot in wells:
            continue
        d = take_demand()
        if d is None:
            break
        wells[slot] = _make_well(slot, cols, d, {"slot": slot, "locked": False, "source": None})

    unplaced = sum(1 for u in used_demand if not u)
    if unplaced > 0:
        add_issue(
            "PLATE_FULL",
            message=(f"布板占用 {len(wells)} 孔，仍有 {unplaced} 个孔位需求无法放置，"
                     "请拖入空孔或删除目标") if explicit_fixed_board
                    else f"孔位不足，{unplaced} 个孔位需求被截断",
        )

    samples = [w for w in wells.values() if w["role"] == "sample"]
    by_level: dict[int, list[dict]] = {}
    for w in samples:
        by_level.setdefault(w["level"], []).append(w)
    for lst in by_level.values():
        lst.sort(key=lambda w: (w["replicate"], w["slot"]))

    # 3) 迭代求解来源链与可达浓度 ---------------------------------------------
    for _ in range(MAX_REPAIR_ROUNDS):
        changed = _assign_sources(wells, by_level, cfg)
        conc_changed = _compute_concentrations(wells, cfg)
        repaired = _repair_unreachable(wells, by_level, cfg)
        if not changed and not conc_changed and not repaired:
            break
    _compute_concentrations(wells, cfg)

    # 4) 深度 / 循环 / 无效来源检查 -------------------------------------------
    _check_graph(wells, cfg, add_issue)

    # 5) 转移量与全部体积校验 --------------------------------------------------
    stock_used = _compute_transfers(wells, cfg, add_issue)
    if cfg["stock_volume"] is not None and stock_used > cfg["stock_volume"] + 1e-6:
        add_issue(
            "STOCK_SHORT",
            slots=[w["slot"] for w in samples if w["source"] == STOCK],
            message=f"母液总需求 {stock_used:.1f} µL 超过备量 {cfg['stock_volume']:.1f} µL，"
                    f"缺口 {stock_used - cfg['stock_volume']:.1f} µL",
            severity=WARNING,
        )

    # 6) 有序台面步骤 ----------------------------------------------------------
    steps = _build_steps(wells, by_level, cfg)
    _index_steps(steps, wells, cols)

    feasible = not any(i["severity"] == ERROR for i in issues)
    return {
        "config": _public_config(cfg),
        "rows": rows,
        "cols": cols,
        "plate_type": cfg["plate_type"],
        "wells": [_public_well(wells[s]) for s in sorted(wells)],
        "steps": steps,
        "issues": issues,
        "feasible": feasible,
        "summary": _summary(wells, steps, cfg, issues, stock_used),
    }


def _public_config(cfg: dict) -> dict:
    return {k: v for k, v in cfg.items() if k != "wells"} | {"wells": cfg["wells"]}


def _make_well(slot: int, cols: int, demand: dict, fixed: dict) -> dict:
    r, c = row_col_of(slot, cols)
    return {
        "slot": slot,
        "label": slot_label(slot, cols),
        "row": r,
        "col": c,
        "role": demand["role"],
        "level": demand["level"],
        "replicate": demand["replicate"],
        "target_conc": demand["target_conc"],
        "final_volume": demand["final_volume"],
        "prep_volume": demand["final_volume"],
        "locked": bool(fixed.get("locked")),
        "manual_source": fixed.get("source"),
        "source": None,
        "source_label": None,
        "source_conc": None,
        "source_liquid_conc": None,
        "computed_conc": None,
        "chain_depth": None,
        "analyte_transfer": None,
        "diluent_transfer": None,
        "deviations": [],
        "issue_codes": [],
    }


def _public_well(w: dict) -> dict:
    return {
        "slot": w["slot"],
        "label": w["label"],
        "row": w["row"],
        "col": w["col"],
        "role": w["role"],
        "level": w["level"],
        "replicate": w["replicate"],
        "target_conc": w["target_conc"],
        "final_volume": w["final_volume"],
        "prep_volume": round(w.get("prep_volume", w["final_volume"]), 3),
        "locked": w["locked"],
        "manual_source": w["manual_source"],
        "source": w["source"],
        "source_label": w["source_label"],
        "source_conc": w["source_conc"],
        "source_liquid_conc": w.get("source_liquid_conc"),
        "computed_conc": w["computed_conc"],
        "chain_depth": w["chain_depth"],
        "analyte_transfer": w["analyte_transfer"],
        "diluent_transfer": w["diluent_transfer"],
        "deviations": w["deviations"],
        "issue_codes": w["issue_codes"],
    }


# --------------------------------------------------------------------------- #
# 来源链求解
# --------------------------------------------------------------------------- #

def _resolve_source(well: dict, wells: dict[int, dict]):
    src = well["source"]
    if src in (STOCK, DILUENT):
        return src
    try:
        slot = int(src)
    except (TypeError, ValueError):
        return None
    return wells.get(slot)


def _parent_candidates(well: dict, by_level: dict[int, list[dict]]) -> list[dict]:
    prev = by_level.get(well["level"] - 1, [])
    same = [w for w in prev if w["replicate"] == well["replicate"]]
    others = [w for w in prev if w["replicate"] != well["replicate"]]
    return same + others


def _basic_feasible(parent_conc: float, child: dict, cfg: dict) -> bool:
    if parent_conc is None or parent_conc <= child["target_conc"] * (1 + 1e-9):
        return False
    t = child["target_conc"] * child["final_volume"] / parent_conc
    return cfg["pipette_min"] <= t <= child["final_volume"] + 1e-9


def _assign_sources(wells, by_level, cfg) -> bool:
    changed = False
    for level in sorted(by_level):
        for w in by_level[level]:
            if w["manual_source"] is not None:
                new_source = w["manual_source"]
            elif level == 0:
                new_source = STOCK
            else:
                chosen = None
                for cand in _parent_candidates(w, by_level):
                    if _basic_feasible(cand["computed_conc"], w, cfg):
                        chosen = cand
                        break
                new_source = chosen["slot"] if chosen else STOCK
            if w["source"] != new_source:
                w["source"] = new_source
                changed = True
    for w in wells.values():
        if w["role"] == "blank" and w["manual_source"] is None:
            if w["source"] != DILUENT:
                w["source"] = DILUENT
                changed = True
    return changed


def _compute_concentrations(wells, cfg) -> bool:
    """沿来源图 DFS 求每孔“可提供浓度”（该孔配制后能给下游的浓度）。

    可达时等于目标浓度；来源浓度不足时为来源可提供的上限：
        offered(w) = min(target_w, offered(来源))
    循环 / 无效来源退回母液。
    """
    state: dict[int, int] = {}
    result: dict[int, Any] = {}

    def visit(w, stack):
        if w["slot"] in state:
            return result.get(w["slot"])
        if w["slot"] in stack:
            return None
        stack = stack | {w["slot"]}
        if w["role"] == "blank":
            state[w["slot"]] = 1
            result[w["slot"]] = 0.0
            return 0.0
        obj = _resolve_source(w, wells)
        if obj is None:
            state[w["slot"]] = 1
            result[w["slot"]] = None
            return None
        if obj == STOCK:
            source_conc = cfg["stock_concentration"]
        elif obj == DILUENT:
            source_conc = 0.0
        else:
            source_conc = visit(obj, stack)
            if source_conc is None:
                state[w["slot"]] = 1
                result[w["slot"]] = None
                return None
        value = min(w["target_conc"], source_conc)
        state[w["slot"]] = 1
        result[w["slot"]] = value
        return value

    changed = False
    for w in wells.values():
        val = visit(w, set())
        if val is None:
            # 循环或无效来源：回退母液作为最接近可执行方案
            val = cfg["stock_concentration"] if w["role"] == "sample" else 0.0
        if w["computed_conc"] is None or abs(w["computed_conc"] - val) > 1e-9:
            changed = True
        w["computed_conc"] = val
    return changed


def _repair_unreachable(wells, by_level, cfg) -> bool:
    """自动来源不可行时，在所有更高级孔中找最低可行来源；否则回退母液。"""
    changed = False
    stock = cfg["stock_concentration"]
    for level in sorted(by_level):
        if level == 0:
            continue
        for w in by_level[level]:
            if w["manual_source"] is not None:
                continue
            obj = _resolve_source(w, wells)
            need_switch = False
            if obj == STOCK:
                need_switch = stock < w["target_conc"] * (1 - 1e-9) or \
                    not _basic_feasible(stock, w, cfg)
            elif isinstance(obj, dict):
                need_switch = not _basic_feasible(obj["computed_conc"], w, cfg)
            elif obj == DILUENT or obj is None:
                need_switch = w["target_conc"] > 0
            if not need_switch:
                continue
            best = None
            for plevel in range(level):
                for cand in by_level.get(plevel, []):
                    if _basic_feasible(cand["computed_conc"], w, cfg):
                        if best is None or cand["computed_conc"] < best["computed_conc"]:
                            best = cand
            if best is not None:
                target = best["slot"]
            elif _basic_feasible(stock, w, cfg):
                target = STOCK
            else:
                target = None
            if target is not None and w["source"] != target:
                w["source"] = target
                changed = True
    return changed


def _check_graph(wells, cfg, add_issue) -> None:
    """来源循环 / 无效来源 / 级数超限检测（仅用于报错，不改变退回方案）。"""
    color: dict[int, int] = {}
    cyclic: set[int] = set()

    def dfs(w, stack):
        if color.get(w["slot"]) == 2:
            return
        if color.get(w["slot"]) == 1:
            cyclic.update(stack[stack.index(w["slot"]):])
            return
        color[w["slot"]] = 1
        stack.append(w["slot"])
        obj = _resolve_source(w, wells)
        if isinstance(obj, dict):
            dfs(obj, stack)
        color[w["slot"]] = 2
        stack.pop()

    for w in wells.values():
        dfs(w, []) if color.get(w["slot"]) != 2 else None

    invalid_slots, cycle_slots = [], []
    for w in wells.values():
        if w["role"] == "blank":
            continue
        obj = _resolve_source(w, wells)
        if obj is None:
            invalid_slots.append(w["slot"])
        elif w["slot"] in cyclic:
            cycle_slots.append(w["slot"])
    if invalid_slots:
        add_issue("SOURCE_INVALID", invalid_slots,
                  "存在指向空孔/自身的无效来源，已回退为母液直接稀释（最接近方案）")
    if cycle_slots:
        add_issue("SOURCE_CYCLE", cycle_slots,
                  "来源链中存在循环（如 A←B←A），已将相关孔回退为母液直接稀释")

    depth: dict[int, int] = {}

    def calc_depth(w, guard):
        if w["slot"] in depth:
            return depth[w["slot"]]
        if w["slot"] in guard:
            return 10 ** 6
        obj = _resolve_source(w, wells)
        if not isinstance(obj, dict):
            d = 1 if obj == STOCK else 0
        else:
            d = calc_depth(obj, guard | {w["slot"]}) + 1
        depth[w["slot"]] = d
        return d

    over = []
    for w in wells.values():
        if w["role"] != "sample":
            continue
        d = calc_depth(w, set())
        w["chain_depth"] = d if d < 10 ** 6 else None
        if d > cfg["max_chain"]:
            over.append(w["slot"])
    if over:
        add_issue("STEP_DEPTH", over,
                  f"有孔连续稀释级数超过允许的 {cfg['max_chain']} 级，"
                  "请调整来源或增大“允许连续稀释级数”",
                  severity=WARNING)


# --------------------------------------------------------------------------- #
# 转移量（物料守恒）与体积校验
# --------------------------------------------------------------------------- #

def _nearest_transfer(volume: float, ranges, limit: float | None = None
                      ) -> tuple[float, list[float], str]:
    """越界时返回量程边界上最接近的可行体积；limit 限制上限（如孔终体积）。"""
    if volume < min(r["min"] for r in ranges):
        # 选最小量程最低的档，把体积提升到其下限
        rng = min(ranges, key=lambda r: r["min"])
        v = rng["min"]
        if limit is not None and v > limit:
            v = limit
            return v, [round(v, 3)], f"受限取 {_fmt(v)} µL（低于最小量程 {_fmt(rng['min'])} µL）"
        return v, [round(v, 3)], f"已提升至最小量程 {_fmt(rng['min'])} µL"
    rng = max(ranges, key=lambda r: r["max"])
    k = max(1, math.ceil(volume / rng["max"]))
    v = k * rng["max"]
    if limit is not None and v > limit:
        v = limit
        k = max(1, math.ceil(v / rng["max"]))
        return v, [round(v / k, 3)] * k, f"受孔体积限制取 {_fmt(v)} µL"
    return v, [round(rng["max"], 3)] * k, f"已满量程分 {k} 次（{_fmt(k * rng['max'])} µL）"


def _compute_transfers(wells, cfg, add_issue) -> float:
    """计算每孔转移量（物料守恒）、来源孔配制体积与全部偏差/问题。

    体积模型：
      - 每孔保留终体积 = 用户目标体积（供下游取液后仍需保留）
      - 作为来源的孔，配制体积 = 终体积 + 供出总量 + 死体积
        （先按目标浓度配好，再用稀释液补足供出/死体积部分，浓度不变）
    返回母液总消耗量。
    """
    ranges = cfg["pipette_ranges"]
    capacity = cfg["capacity"]
    issue_map: dict[str, list[int]] = {}

    def flag(code: str, slot: int):
        if slot not in issue_map.setdefault(code, []):
            issue_map[code].append(slot)
        if code not in wells[slot]["issue_codes"]:
            wells[slot]["issue_codes"].append(code)

    # ---- 第一遍：按终体积计算分析物/稀释液转移量 -------------------------
    raw_analyte: dict[int, float] = {}
    for w in wells.values():
        w["deviations"] = []
        w["issue_codes"] = []
        w["prep_volume"] = w["final_volume"]

        if w["role"] == "blank":
            w["source"] = DILUENT
            w["source_label"] = "稀释液"
            w["source_conc"] = 0.0
            w["analyte_transfer"] = None
            raw_analyte[w["slot"]] = 0.0
            continue

        src_conc = w["computed_conc"]   # 本孔可提供浓度（用于可达性判断）
        obj = _resolve_source(w, wells)
        if obj == STOCK:
            w["source_label"] = "母液"
            liquid_conc = cfg["stock_concentration"]
        elif obj == DILUENT:
            w["source_label"] = "稀释液（异常）"
            liquid_conc = 0.0
        elif isinstance(obj, dict):
            w["source_label"] = obj["label"]
            liquid_conc = obj["computed_conc"]
        else:
            w["source_label"] = "母液（来源无效已回退）"
            liquid_conc = cfg["stock_concentration"]
        w["source_conc"] = src_conc
        w["source_liquid_conc"] = liquid_conc

        eff_final = min(w["final_volume"], capacity)
        if liquid_conc and liquid_conc > 0:
            analyte_v = min(eff_final, w["target_conc"] * eff_final / liquid_conc)
        else:
            analyte_v = 0.0
        raw_analyte[w["slot"]] = analyte_v

        # 浓度不可达
        if src_conc is not None and src_conc < w["target_conc"] * (1 - 1e-9):
            flag("CONC_UNREACHABLE", w["slot"])
            ratio = src_conc / w["target_conc"] if w["target_conc"] else 0
            w["deviations"].append({
                "kind": "concentration",
                "target": w["target_conc"],
                "achieved": round(src_conc, 6),
                "ratio": round(ratio, 4),
                "message": f"来源浓度 {_fmt(src_conc)} {cfg['concentration_unit']} "
                           f"低于目标 {_fmt(w['target_conc'])}（仅达上限的 {ratio * 100:.1f}%）；"
                           "已按来源能达到的最高浓度给最接近方案",
            })

    # ---- 第二遍：累计每个孔的供出量，求配制体积 ---------------------------
    outgoing: dict[int, float] = {s: 0.0 for s in wells}
    for w in wells.values():
        if w["role"] != "sample":
            continue
        obj = _resolve_source(w, wells)
        if isinstance(obj, dict):
            outgoing[obj["slot"]] += raw_analyte[w["slot"]]

    for w in wells.values():
        out = outgoing.get(w["slot"], 0.0)
        if w["role"] == "sample" and out > 1e-9:
            w["prep_volume"] = w["final_volume"] + out + cfg["dead_volume"]
            if w["final_volume"] < cfg["min_mix"] - 1e-9:
                flag("MIN_MIX", w["slot"])
                w["deviations"].append({
                    "kind": "min_mix",
                    "target": w["final_volume"],
                    "achieved": cfg["min_mix"],
                    "message": f"来源孔终体积 {_fmt(w['final_volume'])} µL "
                               f"< 最小混匀体积 {_fmt(cfg['min_mix'])} µL，建议增大终体积",
                })
        else:
            w["prep_volume"] = w["final_volume"]

    # ---- 第三遍：容量校验 + 移液器分次与偏差，生成转移对象 -----------------
    for w in wells.values():
        if w["role"] == "blank":
            v = min(w["final_volume"], capacity)
            if w["final_volume"] > capacity + 1e-6:
                flag("CAPACITY", w["slot"])
                w["deviations"].append(_cap_dev(w["final_volume"], capacity))
            feasible, chunks, note = pipette_check(v, ranges)
            w["diluent_transfer"] = {
                "from": DILUENT, "from_label": "稀释液",
                "volume": round(v, 3), "chunks": chunks, "feasible": feasible, "note": note,
            }
            w["computed_conc"] = 0.0
            if not feasible:
                flag("VOLUME_RANGE", w["slot"])
            continue

        eff_final = min(w["final_volume"], capacity)
        analyte_v = raw_analyte[w["slot"]]
        src_conc = w["source_conc"]
        liquid_conc = w.get("source_liquid_conc") or 0.0

        # 配制体积容量检查
        if w["prep_volume"] > capacity + 1e-6:
            flag("CAPACITY", w["slot"])
            w["deviations"].append({
                "kind": "capacity",
                "target": round(w["prep_volume"], 2),
                "achieved": capacity,
                "message": f"作为来源需配制 {_fmt(w['prep_volume'])} µL（终体积+供出+死体积），"
                           f"超过孔容量 {_fmt(capacity)} µL；请减少下游孔数/平行样或换大板型，"
                           "系统按容量上限给最接近方案",
            })
        elif w["final_volume"] > capacity + 1e-6:
            flag("CAPACITY", w["slot"])
            w["deviations"].append(_cap_dev(w["final_volume"], capacity))

        a_feasible, a_chunks, a_note = pipette_check(analyte_v, ranges)
        repaired_v = analyte_v
        eff_achieved = eff_final  # 实际可达到的终体积（容量/量程受限时可能变小）
        if analyte_v > 1e-9 and not a_feasible:
            pmin = min(r["min"] for r in ranges)
            if analyte_v < pmin and eff_final < pmin - 1e-9:
                # 终体积本身小于最小量程：最小可行方案是按 pmin 配制
                repaired_v = pmin
                eff_achieved = pmin
                a_chunks, a_note = [round(pmin, 3)], f"终体积小于最小量程，按 {_fmt(pmin)} µL 配制"
                flag("CAPACITY", w["slot"])
                w["deviations"].append({
                    "kind": "capacity",
                    "target": round(eff_final, 3),
                    "achieved": pmin,
                    "message": f"目标终体积 {_fmt(eff_final)} µL 小于移液器最小量程 {_fmt(pmin)} µL；"
                               f"最接近方案按 {_fmt(pmin)} µL 配制",
                })
            else:
                repaired_v, a_chunks, a_note = _nearest_transfer(analyte_v, ranges, eff_final)
            flag("VOLUME_RANGE", w["slot"])
            w["deviations"].append({
                "kind": "analyte_volume",
                "target": round(analyte_v, 3),
                "achieved": round(repaired_v, 3),
                "message": f"分析物理论转移量 {_fmt(analyte_v)} µL 越出移液器量程；"
                           f"最接近方案取 {_fmt(repaired_v)} µL，浓度/终体积将偏差",
            })

        diluent_v = max(0.0, eff_achieved - repaired_v)
        d_feasible, d_chunks, d_note = pipette_check(diluent_v, ranges)
        if diluent_v > 1e-9 and not d_feasible:
            repaired_d, d_chunks, d_note = _nearest_transfer(diluent_v, ranges)
            flag("VOLUME_RANGE", w["slot"])
            w["deviations"].append({
                "kind": "diluent_volume",
                "target": round(diluent_v, 3),
                "achieved": round(repaired_d, 3),
                "message": f"稀释液量 {_fmt(diluent_v)} µL 越出移液器量程；"
                           f"最接近方案 {_fmt(repaired_d)} µL，终体积将偏差",
            })
            diluent_v = repaired_d

        # 作为来源时额外补入的稀释液（供出量+死体积），保证配制浓度
        extra = 0.0
        extra_chunks: list[float] = []
        extra_note = ""
        out = outgoing.get(w["slot"], 0.0)
        if out > 1e-9:
            extra = out + cfg["dead_volume"]
            ok, extra_chunks, extra_note = pipette_check(extra, ranges)
            if not ok:
                flag("VOLUME_RANGE", w["slot"])

        # 合并稀释液转移（补至终体积 + 供出/死体积补足），分次列表拼接
        total_diluent = diluent_v + extra
        all_d_chunks = (d_chunks if diluent_v > 1e-9 else []) + list(extra_chunks)
        w["analyte_transfer"] = {
            "from": w["source"],
            "from_label": w["source_label"],
            "volume": round(repaired_v, 3),
            "chunks": a_chunks,
            "feasible": a_feasible,
            "note": a_note,
        }
        if total_diluent > 1e-6:
            note_parts = []
            if diluent_v > 1e-9:
                note_parts.append(f"补至终体积 {_fmt(round(diluent_v, 3))} µL（{d_note}）")
            if extra > 1e-9:
                role = "作为来源，另补供出量与死体积"
                note_parts.append(f"{role} {_fmt(round(extra, 3))} µL（{extra_note}）")
            w["diluent_transfer"] = {
                "from": DILUENT, "from_label": "稀释液",
                "volume": round(total_diluent, 3), "chunks": all_d_chunks,
                "feasible": d_feasible and (extra == 0 or pipette_check(extra, ranges)[0]),
                "note": "；".join(note_parts),
            }
        else:
            w["diluent_transfer"] = None

        # 实际终浓度（守恒）：C = 来源液浓度·Va / 实际终体积
        total = repaired_v + max(0.0, eff_achieved - repaired_v)
        if total > 1e-9 and liquid_conc:
            actual = liquid_conc * repaired_v / eff_achieved
            w["computed_conc"] = round(actual, 6)
            for dv in w["deviations"]:
                if dv["kind"] == "concentration":
                    dv["achieved"] = round(actual, 6)
            if abs(actual - w["target_conc"]) > max(1e-9, w["target_conc"] * 1e-6):
                # 移液量修复同样会造成浓度偏差，补充说明与错误标记
                if not any(d["kind"] == "concentration" for d in w["deviations"]):
                    ratio = actual / w["target_conc"] if w["target_conc"] else 0
                    w["deviations"].append({
                        "kind": "concentration",
                        "target": w["target_conc"],
                        "achieved": round(actual, 6),
                        "ratio": round(ratio, 4),
                        "message": f"因移液量需落在量程内，实际终浓度为 {_fmt(actual)} "
                                   f"{cfg['concentration_unit']}（目标 {_fmt(w['target_conc'])}）",
                    })
                flag("CONC_UNREACHABLE", w["slot"])
        else:
            w["computed_conc"] = 0.0

    for code, slots in issue_map.items():
        severity = WARNING if code in ("MIN_MIX", "STOCK_SHORT", "STEP_DEPTH") else ERROR
        add_issue(code, sorted(slots), severity=severity)

    stock_used = sum(
        w["analyte_transfer"]["volume"]
        for w in wells.values()
        if w["role"] == "sample" and w["analyte_transfer"] and w["source"] == STOCK
    )
    return stock_used


def _cap_dev(target: float, capacity: float) -> dict:
    return {
        "kind": "capacity",
        "target": round(target, 2),
        "achieved": capacity,
        "message": f"目标体积 {_fmt(target)} µL 超过孔容量，"
                   f"最接近方案按 {_fmt(capacity)} µL 执行",
    }


# --------------------------------------------------------------------------- #
# 台面步骤
# --------------------------------------------------------------------------- #

def _transfer_ops(label: str, tr: dict) -> list[dict]:
    ops = []
    chunks = tr.get("chunks") or []
    if not chunks:
        return ops
    for i, v in enumerate(chunks, 1):
        suffix = f"（{i}/{len(chunks)}）" if len(chunks) > 1 else ""
        ops.append({
            "text": f"从 {tr['from_label']} 移取 {_fmt(v)} µL{suffix} 加入 {label}，轻轻吹打混匀",
            "volume": v,
            "from": tr["from"],
            "to_label": label,
        })
    return ops


def _build_steps(wells, by_level, cfg) -> list[dict]:
    """按操作阶段分组：空白孔 → 逐级（先稀释液后分析物，按孔位序）。"""
    steps: list[dict] = []

    blanks = sorted((w for w in wells.values() if w["role"] == "blank"),
                    key=lambda w: w["slot"])
    for w in blanks:
        tr = w["diluent_transfer"]
        if tr:
            for op in _transfer_ops(w["label"], tr):
                steps.append({
                    "phase": "blank",
                    "phase_title": "阶段 0 · 空白孔",
                    "target_slot": w["slot"],
                    **op,
                })

    max_level = max(by_level, default=-1)
    for level in range(max_level + 1):
        group = sorted(by_level.get(level, []), key=lambda w: w["slot"])
        phase = f"level_{level}"
        title = (f"阶段 {level + 1} · 目标 {_fmt(cfg['targets'][level]['concentration'])} "
                 f"{cfg['concentration_unit']}" if level < len(cfg["targets"])
                 else f"阶段 {level + 1}")
        for w in group:
            if w["diluent_transfer"]:
                for op in _transfer_ops(w["label"], w["diluent_transfer"]):
                    steps.append({"phase": phase, "phase_title": title,
                                  "target_slot": w["slot"], **op})
            if w["analyte_transfer"] and w["analyte_transfer"]["volume"] > 1e-9:
                for op in _transfer_ops(w["label"], w["analyte_transfer"]):
                    steps.append({"phase": phase, "phase_title": title,
                                  "target_slot": w["slot"], **op})
    return steps


def _index_steps(steps, wells, cols) -> None:
    for i, s in enumerate(steps, 1):
        s["order"] = i
        slot = s.get("target_slot")
        w = wells.get(slot) if slot is not None else None
        s["target_label"] = w["label"] if w else s.get("to_label")
        s["locked"] = bool(w and w["locked"])


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #

def _summary(wells, steps, cfg, issues, stock_used) -> dict:
    samples = [w for w in wells.values() if w["role"] == "sample"]
    n_error = sum(1 for i in issues if i["severity"] == ERROR)
    n_warn = sum(1 for i in issues if i["severity"] == WARNING)
    diluent_used = 0.0
    for w in wells.values():
        if w["diluent_transfer"]:
            diluent_used += w["diluent_transfer"]["volume"]
    return {
        "well_count": len(wells),
        "sample_count": len(samples),
        "blank_count": sum(1 for w in wells.values() if w["role"] == "blank"),
        "step_count": len(steps),
        "stock_used": round(stock_used, 2),
        "diluent_used": round(diluent_used, 2),
        "errors": n_error,
        "warnings": n_warn,
        "concentration_unit": cfg["concentration_unit"],
    }
