"""摄入质量门禁路由：分区判定、提交发布、中断恢复、迟到更正与质量复核待办。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.quality_gate import quality_gate_service

router = APIRouter(prefix="/api/quality-gate", tags=["摄入质量门禁"])


class PartitionSample(BaseModel):
    partition: str
    valid_rows: int
    total_rows: int
    threshold: float = 0.8
    rule: str = "ratio"
    window_id: str | None = None
    source_refs: list[str] = Field(default_factory=list)
    check_id: int | str | None = None


class EvaluatePayload(BaseModel):
    check_id: int | str
    samples: list[PartitionSample]
    reason: str | None = None


class CorrectPayload(BaseModel):
    partition: str
    valid_rows: int
    total_rows: int
    check_id: int | str | None = None
    source_refs: list[str] | None = None
    note: str | None = None


class SubmitPayload(BaseModel):
    partitions: list[str]
    idempotency_key: str | None = None
    fail_after_scope: bool = False


class RetryPayload(BaseModel):
    partitions: list[str]
    idempotency_key: str | None = None


class ResolveTodoPayload(BaseModel):
    resolution: str | None = None


@router.post("/verdicts")
def evaluate_verdicts(payload: EvaluatePayload) -> dict[str, Any]:
    """按采样窗口阈值和判定规则，逐分区计算摄入质量判定。"""
    return quality_gate_service.evaluate_partition_samples(
        payload.check_id,
        [sample.model_dump() for sample in payload.samples],
        reason=payload.reason or "采样窗口摄入质量判定",
    )


@router.get("/verdicts")
def list_verdicts(partition: str | None = None) -> dict[str, Any]:
    return quality_gate_service.list_verdicts(partition=partition)


@router.post("/verdicts/correct")
def correct_verdict(payload: CorrectPayload) -> dict[str, Any]:
    """迟到行更正：保留原判定历史，新证据与原判定区分登记。"""
    return quality_gate_service.correct_partition_sample(
        payload.partition,
        payload.valid_rows,
        payload.total_rows,
        check_id=payload.check_id,
        source_refs=payload.source_refs,
        note=payload.note or "迟到行更正",
    )


@router.post("/commits/{commit_id}/submit")
def submit_commit(commit_id: int, payload: SubmitPayload) -> dict[str, Any]:
    """提交版本按摄入质量判定生成实际发布范围，同一范围只写入一次。"""
    return quality_gate_service.submit_quality_commit(
        commit_id,
        payload.partitions,
        idempotency_key=payload.idempotency_key,
        fail_after_scope=payload.fail_after_scope,
    )


@router.post("/scopes/{scope_id}/resume")
def resume_scope(scope_id: int) -> dict[str, Any]:
    """中断恢复：保留原样本窗口和待处理分区，已发布分区不重复写入。"""
    return quality_gate_service.resume_quality_commit(scope_id)


@router.get("/commits/{commit_id}/detail")
def commit_detail(commit_id: int) -> dict[str, Any]:
    """版本详情：每个已提交分区可查采用的样本、阈值与判定依据。"""
    return quality_gate_service.get_commit_detail(commit_id)


@router.post("/commits/{commit_id}/retry")
def retry_commit(commit_id: int, payload: RetryPayload) -> dict[str, Any]:
    """更正后重试：只补写此前被阻断的分区，已发布分区不会再次写入。"""
    return quality_gate_service.retry_commit_partitions(
        commit_id,
        payload.partitions,
        idempotency_key=payload.idempotency_key,
    )


@router.get("/review-todos")
def review_todos(
    todo_type: str | None = None,
    status: str | None = None,
    partition: str | None = None,
) -> dict[str, Any]:
    """质量复核待办：已发布分区、新证据、被阻断范围分组可区分。"""
    return quality_gate_service.list_review_todos(
        todo_type=todo_type, status=status, partition=partition
    )


@router.post("/review-todos/{todo_id}/resolve")
def resolve_review_todo(todo_id: int, payload: ResolveTodoPayload) -> dict[str, Any]:
    return quality_gate_service.resolve_review_todo(
        todo_id, payload.resolution or "人工复核确认"
    )
