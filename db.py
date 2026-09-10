"""SQLite 持久化层：实验配置/方案、逐步执行记录与操作日志。"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).with_name("dilution.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS step_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL,
    step_order INTEGER NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    done_at TEXT,
    note TEXT DEFAULT '',
    UNIQUE(experiment_id, step_order),
    FOREIGN KEY(experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS execution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL,
    step_order INTEGER,
    action TEXT NOT NULL,
    detail TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
);
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# 实验
# --------------------------------------------------------------------------- #

def create_experiment(name: str, config: dict, result: dict) -> int:
    now = _now()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO experiments (name, config_json, result_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, json.dumps(config, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), now, now),
        )
        exp_id = cur.lastrowid
        _sync_step_states(conn, exp_id, result)
        conn.execute(
            "INSERT INTO execution_log (experiment_id, step_order, action, detail, created_at)"
            " VALUES (?, NULL, 'create', ?, ?)",
            (exp_id, f"创建实验「{name}」，生成 {len(result.get('steps', []))} 个步骤", now),
        )
    return exp_id


def update_experiment(exp_id: int, name: str, config: dict, result: dict) -> None:
    now = _now()
    with get_db() as conn:
        conn.execute(
            "UPDATE experiments SET name=?, config_json=?, result_json=?, updated_at=? WHERE id=?",
            (name, json.dumps(config, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), now, exp_id),
        )
        _sync_step_states(conn, exp_id, result)
        conn.execute(
            "INSERT INTO execution_log (experiment_id, step_order, action, detail, created_at)"
            " VALUES (?, NULL, 'replan', '参数或布板变化，已重新计算方案', ?)",
            (exp_id, now),
        )


def _sync_step_states(conn, exp_id: int, result: dict) -> None:
    """方案重算后保留已勾选步骤（按 order 对齐），补齐新增、删除失效。"""
    orders = [s["order"] for s in result.get("steps", [])]
    existing = {
        row["step_order"]
        for row in conn.execute(
            "SELECT step_order FROM step_states WHERE experiment_id=?", (exp_id,)
        )
    }
    for order in orders:
        if order not in existing:
            conn.execute(
                "INSERT INTO step_states (experiment_id, step_order, done) VALUES (?, ?, 0)",
                (exp_id, order),
            )
    if existing:
        placeholders = ",".join("?" for _ in orders) if orders else "-1"
        conn.execute(
            f"DELETE FROM step_states WHERE experiment_id=? "
            f"AND step_order NOT IN ({placeholders})",
            (exp_id, *orders),
        )


def list_experiments() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at, updated_at FROM experiments ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_experiment(exp_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM experiments WHERE id=?", (exp_id,)
        ).fetchone()
        if row is None:
            return None
        exp = dict(row)
        exp["config"] = json.loads(exp.pop("config_json"))
        exp["result"] = json.loads(exp.pop("result_json"))
        steps = conn.execute(
            "SELECT step_order, done, done_at, note FROM step_states "
            "WHERE experiment_id=? ORDER BY step_order",
            (exp_id,),
        ).fetchall()
        exp["step_states"] = {s["step_order"]: dict(s) for s in steps}
        exp["logs"] = [dict(r) for r in conn.execute(
            "SELECT step_order, action, detail, created_at FROM execution_log "
            "WHERE experiment_id=? ORDER BY id", (exp_id,)
        ).fetchall()]
        return exp


def delete_experiment(exp_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM experiments WHERE id=?", (exp_id,))


# --------------------------------------------------------------------------- #
# 执行记录
# --------------------------------------------------------------------------- #

def set_step(exp_id: int, order: int, done: bool, note: str | None = None) -> dict:
    now = _now()
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM step_states WHERE experiment_id=? AND step_order=?",
            (exp_id, order),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO step_states (experiment_id, step_order, done, done_at, note)"
                " VALUES (?, ?, ?, ?, ?)",
                (exp_id, order, int(done), now if done else None, note or ""),
            )
        else:
            conn.execute(
                "UPDATE step_states SET done=?, done_at=?, note=? "
                "WHERE experiment_id=? AND step_order=?",
                (int(done), now if done else None, note if note is not None else "",
                 exp_id, order),
            )
        conn.execute(
            "INSERT INTO execution_log (experiment_id, step_order, action, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (exp_id, order, "check" if done else "uncheck",
             f"步骤 #{order} {'完成' if done else '取消完成'}", now),
        )
        state = conn.execute(
            "SELECT step_order, done, done_at, note FROM step_states "
            "WHERE experiment_id=? AND step_order=?",
            (exp_id, order),
        ).fetchone()
        return dict(state)


def reset_steps(exp_id: int) -> None:
    now = _now()
    with get_db() as conn:
        conn.execute(
            "UPDATE step_states SET done=0, done_at=NULL, note='' WHERE experiment_id=?",
            (exp_id,),
        )
        conn.execute(
            "INSERT INTO execution_log (experiment_id, step_order, action, detail, created_at)"
            " VALUES (?, NULL, 'reset', '重置全部执行勾选', ?)",
            (exp_id, now),
        )
