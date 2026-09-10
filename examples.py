"""内置示例实验（所有数据均为本地静态数据）。"""

EXAMPLES = [
    {
        "id": "example-96-serial",
        "name": "示例 1 · 96 孔板 10 倍系列稀释（含平行样与空白）",
        "description": "100 µM 母液，1:10 系列稀释 6 个浓度点，每点 2 个平行，"
                       "外加 2 个空白孔；单孔终体积 100 µL，2–20/20–200 µL 双档移液器。",
        "config": {
            "plate_type": 96,
            "stock_concentration": 100,
            "stock_volume": 5000,
            "concentration_unit": "µM",
            "well_capacity": 280,
            "pipette_ranges": [{"min": 2, "max": 20}, {"min": 20, "max": 200}],
            "min_mix": 50,
            "dead_volume": 20,
            "max_chain": 12,
            "blank_count": 2,
            "blank_volume": 100,
            "targets": [
                {"concentration": 10, "volume": 100, "replicates": 2},
                {"concentration": 1, "volume": 100, "replicates": 2},
                {"concentration": 0.1, "volume": 100, "replicates": 2},
                {"concentration": 0.01, "volume": 100, "replicates": 2},
                {"concentration": 0.001, "volume": 100, "replicates": 2},
                {"concentration": 0.0001, "volume": 100, "replicates": 2},
            ],
            "wells": [],
        },
    },
    {
        "id": "example-24-half-log",
        "name": "示例 2 · 24 孔板半数稀释（IC50 摸索）",
        "description": "2 mM 母液，按 1/2 逐级稀释 8 个浓度点，无平行，"
                       "终体积 500 µL；20–200 µL 单档移液器，死体积 100 µL。",
        "config": {
            "plate_type": 24,
            "stock_concentration": 2000,
            "stock_volume": 3000,
            "concentration_unit": "nM",
            "well_capacity": 2900,
            "pipette_ranges": [{"min": 20, "max": 200}, {"min": 200, "max": 1000}],
            "min_mix": 200,
            "dead_volume": 100,
            "max_chain": 12,
            "blank_count": 1,
            "blank_volume": 500,
            "targets": [
                {"concentration": 1000, "volume": 500, "replicates": 1},
                {"concentration": 500, "volume": 500, "replicates": 1},
                {"concentration": 250, "volume": 500, "replicates": 1},
                {"concentration": 125, "volume": 500, "replicates": 1},
                {"concentration": 62.5, "volume": 500, "replicates": 1},
                {"concentration": 31.25, "volume": 500, "replicates": 1},
                {"concentration": 15.625, "volume": 500, "replicates": 1},
                {"concentration": 7.8125, "volume": 500, "replicates": 1},
            ],
            "wells": [],
        },
    },
    {
        "id": "example-96-infeasible",
        "name": "示例 3 · 含冲突与越界（演示自动检查与最接近方案）",
        "description": "母液仅 50 µM 却要求 80 µL 终体积的 80 µM 孔（浓度不可达），"
                       "另有 0.5 µL 移液（低于 2 µL 最小量程）与 400 µL 超容量孔，"
                       "用于演示越界、容量、不可达提示及修复方案。",
        "config": {
            "plate_type": 96,
            "stock_concentration": 50,
            "stock_volume": 1000,
            "concentration_unit": "µg/mL",
            "well_capacity": 280,
            "pipette_ranges": [{"min": 2, "max": 20}, {"min": 20, "max": 200}],
            "min_mix": 50,
            "dead_volume": 20,
            "max_chain": 3,
            "blank_count": 1,
            "blank_volume": 400,
            "targets": [
                {"concentration": 80, "volume": 80, "replicates": 1},
                {"concentration": 10, "volume": 0.5, "replicates": 1},
                {"concentration": 1, "volume": 100, "replicates": 1},
                {"concentration": 0.1, "volume": 100, "replicates": 1},
                {"concentration": 0.01, "volume": 100, "replicates": 1},
                {"concentration": 0.001, "volume": 100, "replicates": 1},
            ],
            "wells": [],
        },
    },
]


def get_example(example_id: str):
    for ex in EXAMPLES:
        if ex["id"] == example_id:
            return ex
    return None
