"""微孔板梯度稀释规划器 —— 本地 Flask 应用。

所有计算均在本机由 planner.py 完成，SQLite 仅做本地持久化。
"""

import csv
import io

from flask import Flask, Response, jsonify, render_template, request

import db
import planner
from examples import EXAMPLES, get_example

app = Flask(__name__)
db.init_db()


@app.get("/")
def index():
    return render_template("index.html")


# --------------------------------------------------------------------------- #
# 规划（不落库，供前端实时重算）
# --------------------------------------------------------------------------- #

@app.post("/api/plan")
def api_plan():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(planner.plan(data.get("config") or data))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/examples")
def api_examples():
    return jsonify([{"id": e["id"], "name": e["name"],
                     "description": e["description"], "config": e["config"]}
                    for e in EXAMPLES])


@app.get("/api/examples/<example_id>")
def api_example(example_id):
    ex = get_example(example_id)
    if not ex:
        return jsonify({"error": "示例不存在"}), 404
    return jsonify(ex)


# --------------------------------------------------------------------------- #
# 实验持久化
# --------------------------------------------------------------------------- #

@app.get("/api/experiments")
def api_list_experiments():
    return jsonify(db.list_experiments())


@app.post("/api/experiments")
def api_create_experiment():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "未命名实验").strip()[:100]
    config = data.get("config") or {}
    try:
        result = planner.plan(config)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    exp_id = db.create_experiment(name, config, result)
    return jsonify({"id": exp_id, "result": result}), 201


@app.get("/api/experiments/<int:exp_id>")
def api_get_experiment(exp_id):
    exp = db.get_experiment(exp_id)
    if not exp:
        return jsonify({"error": "实验不存在"}), 404
    return jsonify(exp)


@app.put("/api/experiments/<int:exp_id>")
def api_update_experiment(exp_id):
    if not db.get_experiment(exp_id):
        return jsonify({"error": "实验不存在"}), 404
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "未命名实验").strip()[:100]
    config = data.get("config") or {}
    try:
        result = planner.plan(config)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    db.update_experiment(exp_id, name, config, result)
    return jsonify({"id": exp_id, "result": result})


@app.delete("/api/experiments/<int:exp_id>")
def api_delete_experiment(exp_id):
    db.delete_experiment(exp_id)
    return jsonify({"ok": True})


@app.post("/api/experiments/<int:exp_id>/steps/<int:order>")
def api_toggle_step(exp_id, order):
    if not db.get_experiment(exp_id):
        return jsonify({"error": "实验不存在"}), 404
    data = request.get_json(silent=True) or {}
    done = bool(data.get("done"))
    note = data.get("note")
    return jsonify(db.set_step(exp_id, order, done, note))


@app.post("/api/experiments/<int:exp_id>/reset")
def api_reset_steps(exp_id):
    if not db.get_experiment(exp_id):
        return jsonify({"error": "实验不存在"}), 404
    db.reset_steps(exp_id)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #

def _result_for_export(exp_id=None, config=None):
    if exp_id is not None:
        exp = db.get_experiment(exp_id)
        if not exp:
            return None, None
        return exp["result"], exp
    try:
        return planner.plan(config or {}), None
    except ValueError:
        return None, None


@app.get("/api/experiments/<int:exp_id>/export.csv")
def export_csv_saved(exp_id):
    result, _ = _result_for_export(exp_id=exp_id)
    if result is None:
        return jsonify({"error": "实验不存在"}), 404
    return _csv_response(result, f"experiment_{exp_id}")


@app.post("/api/export.csv")
def export_csv_adhoc():
    data = request.get_json(silent=True) or {}
    result, _ = _result_for_export(config=data.get("config") or data)
    if result is None:
        return jsonify({"error": "配置无效，无法导出"}), 400
    return _csv_response(result, "plan")


def _csv_response(result: dict, filename: str) -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    unit = result["summary"]["concentration_unit"]

    writer.writerow([f"# {result['plate_type']} 孔板梯度稀释工作单"])
    writer.writerow(["# 一、孔位明细"])
    writer.writerow(["孔位", "角色", "浓度级别", "平行样", f"目标浓度({unit})",
                     f"终浓度({unit})", "终体积(µL)", "配制体积(µL)", "来源",
                     "分析物加入(µL)", "稀释液加入(µL)", "执行步骤", "状态/偏差"])
    order_map = {}
    for s in result["steps"]:
        order_map.setdefault(s["target_slot"], []).append(str(s["order"]))
    for w in result["wells"]:
        deviations = "；".join(d["message"] for d in w.get("deviations", []))
        codes = ",".join(w.get("issue_codes", []))
        status = " | ".join(x for x in (codes, deviations) if x)
        writer.writerow([
            w["label"],
            "空白" if w["role"] == "blank" else "样品",
            "-" if w["level"] < 0 else w["level"] + 1,
            w.get("replicate") or "-",
            w["target_conc"] if w["role"] == "sample" else 0,
            _num(w.get("computed_conc")),
            w["final_volume"],
            _num(w.get("prep_volume")),
            w.get("source_label") or "",
            _num((w.get("analyte_transfer") or {}).get("volume")),
            _num((w.get("diluent_transfer") or {}).get("volume")),
            ",".join(order_map.get(w["slot"], [])),
            status,
        ])

    writer.writerow([])
    writer.writerow(["# 二、台面操作步骤（按顺序执行）"])
    writer.writerow(["步骤", "阶段", "目标孔", "操作", "体积(µL)", "来源"])
    for s in result["steps"]:
        writer.writerow([s["order"], s["phase_title"], s.get("target_label"),
                         s["text"], _num(s.get("volume")), s.get("from")])

    writer.writerow([])
    writer.writerow(["# 三、问题与偏差"])
    if result["issues"]:
        for i in result["issues"]:
            writer.writerow([i["severity"], i["code"], i["message"],
                             " ".join(planner.slot_label(s, result["cols"])
                                      for s in i["slots"])])
    else:
        writer.writerow(["无"])

    summary = result["summary"]
    writer.writerow([])
    writer.writerow(["# 四、物料汇总"])
    writer.writerow(["母液总消耗(µL)", summary["stock_used"]])
    writer.writerow(["稀释液总消耗(µL)", summary["diluent_used"]])
    writer.writerow(["样品孔数", summary["sample_count"]])
    writer.writerow(["空白孔数", summary["blank_count"]])
    writer.writerow(["错误/警告", f"{summary['errors']} / {summary['warnings']}"])

    return Response(
        "﻿" + buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}.csv"},
    )


def _num(v):
    if v is None:
        return ""
    return round(float(v), 3)


@app.get("/api/experiments/<int:exp_id>/worksheet")
def worksheet_saved(exp_id):
    exp = db.get_experiment(exp_id)
    if not exp:
        return jsonify({"error": "实验不存在"}), 404
    return _worksheet_response(exp["result"], exp)


@app.post("/api/worksheet")
def worksheet_adhoc():
    data = request.get_json(silent=True) or {}
    result, _ = _result_for_export(config=data.get("config") or data)
    if result is None:
        return jsonify({"error": "配置无效，无法导出"}), 400
    return _worksheet_response(result, None)


def _worksheet_response(result: dict, exp: dict | None) -> Response:
    # 预计算步骤分组标记与问题孔标签（避免模板内变量作用域问题）
    phase_seen = set()
    for s in result["steps"]:
        s["phase_start"] = s["phase"] not in phase_seen
        phase_seen.add(s["phase"])
    slot_labels = {w["slot"]: w["label"] for w in result["wells"]}
    well_steps: dict[int, list[int]] = {}
    for s in result["steps"]:
        well_steps.setdefault(s["target_slot"], []).append(s["order"])
    for w in result["wells"]:
        w["step_orders"] = well_steps.get(w["slot"], [])
    for i in result["issues"]:
        i["slot_labels"] = [slot_labels.get(s, str(s)) for s in i["slots"]]
    html = render_template("worksheet.html", r=result,
                           exp=exp, name=(exp or {}).get("name", "未保存方案"))
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
