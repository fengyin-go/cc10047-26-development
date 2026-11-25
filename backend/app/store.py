"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import copy
import threading
from typing import Any

from app.seed import SEED_ROWS

# 摄入质量门禁领域表：不属于 18 个通用业务模块，由质量门禁服务专用。
QUALITY_GATE_TABLES = (
    "quality_verdicts",        # 分区摄入质量判定（含冻结样本与历史版本）
    "commit_scopes",           # 提交批次的范围、状态与幂等键
    "commit_publish_records",  # 每个已提交分区的实际发布记录
    "quality_review_todos",    # 质量复核待办（已发布/被阻断/新证据）
)


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        for name in QUALITY_GATE_TABLES:
            self._tables[name] = []
        # 进程内互斥锁：并发提交同一范围时保证判定与写入只发生一次。
        self._gate_lock = threading.RLock()
        # 幂等键 -> 已完成提交的响应快照，重复提交直接返回同一结果。
        self._idempotency: dict[str, dict[str, Any]] = {}

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def gate_lock(self) -> threading.RLock:
        """质量门禁写操作统一持有的锁。"""
        return self._gate_lock

    def cached_submission(self, idempotency_key: str) -> dict[str, Any] | None:
        cached = self._idempotency.get(idempotency_key)
        return copy.deepcopy(cached) if cached is not None else None

    def remember_submission(self, idempotency_key: str, result: dict[str, Any]) -> None:
        self._idempotency[idempotency_key] = copy.deepcopy(result)

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            if name in QUALITY_GATE_TABLES:
                continue
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
