"""JSON 文件持久化。

- state.json：当前规划状态（目标、收支、计划扣款、审计等）
- ledger 虽在 state 中，但语义 append-only，store 层不提供修改/删除条目接口
- 所有写操作在进程内锁内完成，并原子替换文件
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

STATE_FILENAME = "state.json"


def default_state() -> dict[str, Any]:
    return {
        "config": {
            "plan_start_month": "",
            "plan_end_month": "",
            "default_currency": "CNY",
            "default_tz": "Asia/Shanghai",
            "transfer_day": 5,                 # 每月自动转账日
            "cutoff_hour": 17,
            "holidays": [],                    # ISO 日期列表
            "priority_overrides": {},          # goal_id -> 优先级（数字越小越高）
        },
        "goals": {},
        "incomes": {},                         # key "month|account"
        "fixed_expenses": {},
        "one_off_expenses": {},
        "training_phases": {},
        "races": {},
        "ledger": [],
        "deductions": {},
        "imports": {},                         # dedupe_key -> {batch, ledger_id}
        "adjustments": [],
        "plan_version": 0,
        "exports": [],
        "last_projection": {"eta": {}},
    }


class Store:
    def __init__(self, runtime_dir: str | Path = ".runtime") -> None:
        self.dir = Path(runtime_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "exports").mkdir(exist_ok=True)
        self.path = self.dir / STATE_FILENAME
        self._lock = threading.RLock()
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as fh:
                state = json.load(fh)
            base = default_state()
            # 前向兼容：补齐缺失顶层键
            for key, value in base.items():
                state.setdefault(key, value)
            return state
        return default_state()

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self.state, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.path)

    def lock(self) -> threading.RLock:
        return self._lock

    # -- 导出快照 -----------------------------------------------------------
    def write_export(self, version: int, snapshot: dict[str, Any]) -> str:
        rel = Path("exports") / f"plan_v{version}.json"
        target = self.dir / rel
        tmp = target.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, target)
        return str(rel)
