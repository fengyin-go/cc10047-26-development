"""流质量校验领域服务：通用字段/状态/动作骨架 + 分区摄入质量判定桥接。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.quality_gate import quality_gate_service


class StreamQualityService(BaseModuleService):
    MODULE = "stream_quality"
    ENTITY = "质量检查"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["检查标识", "目标管道", "校验字段", "校验算子", "告警阈值", "采样窗口", "阻断级别", "上次结果"]
    REQUIRED_FIELDS = ["检查标识", "目标管道", "校验字段"]
    STATUS_ORDER = ["待启用", "运行中", "已通过", "未通过"]
    ACTION_RULES = {"启动检查": "待启用", "忽略告警": "未通过"}
    NEGATIVE_ACTIONS = ["忽略告警"]

    def domain_evaluate_partition_samples(
        self,
        check_id: int,
        samples: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """把来源分区样本数据计算成可用于写入的摄入质量判定数据。

        每个分区按各自采样窗口的阈值和判定规则独立计算有效率，
        合格数、输入行数与实际来源随判定一并落库，供提交版本发布使用。
        """
        entry = self.get_entry(check_id)
        if entry is None:
            return {"ok": False, "message": f"质量检查 {check_id} 不存在"}
        result = quality_gate_service.evaluate_partition_samples(
            entry["id"], samples, reason=f"质量检查 {entry['检查标识']} 分区判定"
        )
        if result.get("ok"):
            entry["上次结果"] = (
                f"可发布分区 {len(result['publishable_partitions'])}/"
                f"{len(result['verdicts'])}"
            )
        return result
