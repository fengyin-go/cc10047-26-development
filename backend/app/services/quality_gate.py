"""摄入质量门禁领域服务。

把流质量校验的来源分区样本计算成可写入的摄入质量判定数据，并驱动提交版本的
实际发布范围：

1. 分区判定（evaluate_partition_samples）：按采样窗口的阈值和判定规则记录
   合格数、输入行数、实际来源与冻结样本，每个分区独立计算有效率，互不抵消；
2. 提交发布（submit_quality_commit）：提交版本只发布判定通过的分区，发布结果
   同步到质量复核待办；同一范围并发提交只写入一次，中断后可恢复且保留原样本
   窗口和待处理分区；
3. 迟到更正（correct_partition_sample）：保留原判定历史，新版本与原判定/已发布
   冻结样本发生冲突时登记「新证据」待办，已发布分区不会被二次写入；
4. 版本详情（get_commit_detail）：每个已提交分区都能查到采用的样本和阈值。
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from app.compute import (
    detect_sample_conflict,
    ingest_quality_partition_verdict,
    select_publishable_partitions,
)
from app.store import store

VERDICT_TABLE = "quality_verdicts"
SCOPE_TABLE = "commit_scopes"
PUBLISH_TABLE = "commit_publish_records"
TODO_TABLE = "quality_review_todos"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sample_fingerprint(sample: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "window_id": sample["window_id"],
            "valid_rows": sample["valid_rows"],
            "total_rows": sample["total_rows"],
            "threshold": sample["threshold"],
            "rule": sample["rule"],
            "source_refs": sample["source_refs"],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _default_idempotency_key(commit_id: int, partitions: list[str]) -> str:
    ordered = ",".join(sorted(str(p) for p in partitions))
    digest = hashlib.sha256(ordered.encode("utf-8")).hexdigest()[:12]
    return f"commit-{commit_id}-{digest}"


def _normalize_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for sample in samples:
        partition = str(sample["partition"])
        threshold = float(sample.get("threshold", 0.8))
        normalized.append(
            {
                "partition": partition,
                "valid_rows": int(sample.get("valid_rows", 0)),
                "total_rows": int(sample.get("total_rows", 0)),
                "threshold": threshold,
                "rule": str(sample.get("rule", "ratio")),
                "window_id": str(sample.get("window_id") or f"win-{partition}"),
                "source_refs": [str(ref) for ref in (sample.get("source_refs") or [])],
                "check_id": sample.get("check_id"),
            }
        )
    return normalized


def _active_verdict(partition: str) -> dict[str, Any] | None:
    for row in store.rows(VERDICT_TABLE):
        if row["partition"] == partition and row["status"] == "active":
            return row
    return None


def _verdict_view(row: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(row)


def _record_view(row: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(row)


def _scope_view(row: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(row)


def _build_verdict_entry(
    sample: dict[str, Any],
    calc: dict[str, Any],
    *,
    previous: dict[str, Any] | None,
    reason: str,
) -> dict[str, Any]:
    entry = {
        "id": store.next_id(VERDICT_TABLE),
        "check_id": sample.get("check_id"),
        "partition": sample["partition"],
        "window_id": sample["window_id"],
        "threshold": sample["threshold"],
        "rule": sample["rule"],
        "valid_rows": calc["valid_rows"],
        "total_rows": calc["total_rows"],
        "valid_rate": calc["valid_rate"],
        "passed": calc["passed"],
        "decision_reason": calc["reason"],
        "source_refs": list(sample["source_refs"]),
        "sample_snapshot": {
            "partition": sample["partition"],
            "window_id": sample["window_id"],
            "valid_rows": calc["valid_rows"],
            "total_rows": calc["total_rows"],
            "threshold": sample["threshold"],
            "rule": sample["rule"],
            "source_refs": list(sample["source_refs"]),
            "passed": calc["passed"],
        },
        "window_fingerprint": _sample_fingerprint(sample),
        "version": (previous["version"] + 1) if previous else 1,
        "status": "active",
        "superseded_by": None,
        "conflict_with_published": False,
        "history": [],
        "reason": reason,
        "created_at": _now(),
    }
    if previous is not None:
        entry["history"] = list(previous.get("history") or []) + [
            {
                "verdict_id": previous["id"],
                "window_id": previous["window_id"],
                "valid_rows": previous["valid_rows"],
                "total_rows": previous["total_rows"],
                "valid_rate": previous["valid_rate"],
                "passed": previous["passed"],
                "threshold": previous["threshold"],
                "rule": previous["rule"],
                "source_refs": list(previous.get("source_refs") or []),
                "created_at": previous["created_at"],
            }
        ]
    return entry


def _supersede(previous: dict[str, Any], new_id: int) -> None:
    previous["status"] = "superseded"
    previous["superseded_by"] = new_id


def _add_review_todo(
    *,
    todo_type: str,
    partition: str,
    commit_id: int | None,
    scope_id: int | None,
    verdict_id: int | None,
    sample_window_id: str | None,
    threshold: float | None,
    valid_rows: int,
    total_rows: int,
    valid_rate: float,
    passed: bool,
    source_refs: list[str],
    message: str,
    conflict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    todo = {
        "id": store.next_id(TODO_TABLE),
        "todo_type": todo_type,
        "status": "open",
        "partition": partition,
        "commit_id": commit_id,
        "scope_id": scope_id,
        "verdict_id": verdict_id,
        "sample_window_id": sample_window_id,
        "threshold": threshold,
        "valid_rows": valid_rows,
        "total_rows": total_rows,
        "valid_rate": valid_rate,
        "passed": passed,
        "source_refs": list(source_refs),
        "conflict": copy.deepcopy(conflict),
        "message": message,
        "resolution": None,
        "created_at": _now(),
        "resolved_at": None,
    }
    store.rows(TODO_TABLE).append(todo)
    return todo


def _published_for_commit(commit_id: int, partition: str) -> dict[str, Any] | None:
    for record in store.rows(PUBLISH_TABLE):
        if record["commit_id"] == commit_id and record["partition"] == partition:
            return record
    return None


def _latest_published(partition: str) -> dict[str, Any] | None:
    matches = [
        record
        for record in store.rows(PUBLISH_TABLE)
        if record["partition"] == partition and record["published"]
    ]
    return matches[-1] if matches else None


class QualityGateService:
    """摄入质量门禁：分区判定 → 版本发布范围 → 质量复核待办。"""

    # ------------------------------------------------------------------
    # 分区判定
    # ------------------------------------------------------------------
    def evaluate_partition_samples(
        self,
        check_id: int | str,
        samples: list[dict[str, Any]],
        *,
        reason: str = "采样窗口摄入质量判定",
    ) -> dict[str, Any]:
        normalized = _normalize_samples(samples)
        if not normalized:
            return {"ok": False, "message": "至少需要一个分区的采样数据"}
        with store.gate_lock():
            verdicts: list[dict[str, Any]] = []
            for sample in normalized:
                calc = ingest_quality_partition_verdict(
                    sample["valid_rows"],
                    sample["total_rows"],
                    sample["threshold"],
                    rule=sample["rule"],
                    partition=sample["partition"],
                )
                active = _active_verdict(sample["partition"])
                fingerprint = _sample_fingerprint(sample)
                # 同窗口同样本重复判定：幂等返回原判定，不产生新版本。
                if (
                    active is not None
                    and active["window_fingerprint"] == fingerprint
                    and active["check_id"] == check_id
                ):
                    verdicts.append(_verdict_view(active))
                    continue
                entry = _build_verdict_entry(sample, calc, previous=active, reason=reason)
                entry["check_id"] = check_id
                if active is not None:
                    _supersede(active, entry["id"])
                store.rows(VERDICT_TABLE).append(entry)
                verdicts.append(_verdict_view(entry))
            return {
                "ok": True,
                "check_id": check_id,
                "publishable_partitions": select_publishable_partitions(verdicts),
                "verdicts": verdicts,
            }

    def list_verdicts(self, partition: str | None = None) -> dict[str, Any]:
        with store.gate_lock():
            rows = store.rows(VERDICT_TABLE)
            if partition is not None:
                rows = [row for row in rows if row["partition"] == partition]
            return {"ok": True, "items": [_verdict_view(row) for row in rows]}

    # ------------------------------------------------------------------
    # 迟到更正：保留原判定历史，新证据与原判定/已发布冻结样本区分开
    # ------------------------------------------------------------------
    def correct_partition_sample(
        self,
        partition: str,
        valid_rows: int,
        total_rows: int,
        *,
        check_id: int | str | None = None,
        source_refs: list[str] | None = None,
        note: str = "迟到行更正",
    ) -> dict[str, Any]:
        partition = str(partition)
        with store.gate_lock():
            active = _active_verdict(partition)
            if active is None:
                return {"ok": False, "message": f"分区 {partition} 还没有摄入质量判定，无法更正"}
            # 中断恢复要求保留原样本窗口：更正沿用原 window_id。
            new_sample = {
                "partition": partition,
                "valid_rows": int(valid_rows),
                "total_rows": int(total_rows),
                "threshold": active["threshold"],
                "rule": active["rule"],
                "window_id": active["window_id"],
                "source_refs": list(
                    source_refs if source_refs is not None else active["source_refs"]
                ),
                "check_id": check_id or active["check_id"],
            }
            calc = ingest_quality_partition_verdict(
                new_sample["valid_rows"],
                new_sample["total_rows"],
                new_sample["threshold"],
                rule=new_sample["rule"],
                partition=partition,
            )
            new_sample["passed"] = calc["passed"]
            fingerprint = _sample_fingerprint(new_sample)
            if fingerprint == active["window_fingerprint"]:
                return {
                    "ok": True,
                    "message": "更正样本与当前判定一致，未产生新版本",
                    "verdict": _verdict_view(active),
                    "conflict": None,
                }
            conflict = detect_sample_conflict(active["sample_snapshot"], new_sample)
            entry = _build_verdict_entry(
                new_sample, calc, previous=active, reason=note
            )
            entry["check_id"] = new_sample["check_id"]
            published = _latest_published(partition)
            if published is not None:
                published_conflict = detect_sample_conflict(
                    published["sample_snapshot"], new_sample
                )
                if published_conflict["conflict"]:
                    entry["conflict_with_published"] = True
                    conflict = published_conflict
            _supersede(active, entry["id"])
            store.rows(VERDICT_TABLE).append(entry)
            if conflict["conflict"]:
                related_scope = next(
                    (
                        scope
                        for scope in reversed(store.rows(SCOPE_TABLE))
                        if partition in scope.get("requested_partitions", [])
                    ),
                    None,
                )
                _add_review_todo(
                    todo_type="new_evidence",
                    partition=partition,
                    commit_id=related_scope["commit_id"] if related_scope else None,
                    scope_id=related_scope["id"] if related_scope else None,
                    verdict_id=entry["id"],
                    sample_window_id=entry["window_id"],
                    threshold=entry["threshold"],
                    valid_rows=entry["valid_rows"],
                    total_rows=entry["total_rows"],
                    valid_rate=entry["valid_rate"],
                    passed=entry["passed"],
                    source_refs=entry["source_refs"],
                    message=(
                        f"分区 {partition} 迟到更正产生新证据：原判定 "
                        f"{conflict['frozen_sample'].get('valid_rows')}/"
                        f"{conflict['frozen_sample'].get('total_rows')} → 当前 "
                        f"{entry['valid_rows']}/{entry['total_rows']}"
                    ),
                    conflict=conflict,
                )
            return {
                "ok": True,
                "message": note,
                "verdict": _verdict_view(entry),
                "previous_verdict": _verdict_view(active),
                "conflict": conflict,
            }

    # ------------------------------------------------------------------
    # 提交版本：按判定数据生成实际发布范围
    # ------------------------------------------------------------------
    def submit_quality_commit(
        self,
        commit_id: int,
        partitions: list[str],
        *,
        idempotency_key: str | None = None,
        fail_after_scope: bool = False,
    ) -> dict[str, Any]:
        partitions = [str(p) for p in partitions]
        if not partitions:
            return {"ok": False, "message": "提交范围不能为空"}
        if store.find("delta_commit", commit_id) is None:
            return {"ok": False, "message": f"提交版本 {commit_id} 不存在"}
        key = idempotency_key or _default_idempotency_key(commit_id, partitions)
        with store.gate_lock():
            cached = store.cached_submission(key)
            if cached is not None:
                cached["idempotent_replay"] = True
                cached["message"] = "同一范围已提交过，本次为幂等重放，未重复写入"
                return cached
            frozen_samples: dict[str, dict[str, Any]] = {}
            for partition in partitions:
                active = _active_verdict(partition)
                if active is not None:
                    frozen_samples[partition] = {
                        "verdict_id": active["id"],
                        "window_id": active["window_id"],
                        "threshold": active["threshold"],
                        "valid_rows": active["valid_rows"],
                        "total_rows": active["total_rows"],
                    }
            scope = {
                "id": store.next_id(SCOPE_TABLE),
                "scope_key": key,
                "commit_id": commit_id,
                "requested_partitions": list(partitions),
                "pending_partitions": list(partitions),
                "published_partitions": [],
                "blocked_partitions": [],
                "frozen_samples": frozen_samples,
                "sample_window_ids": {p: s["window_id"] for p, s in frozen_samples.items()},
                "status": "in_progress",
                "interrupted": False,
                "created_at": _now(),
                "updated_at": _now(),
            }
            store.rows(SCOPE_TABLE).append(scope)
            if fail_after_scope:
                # 模拟写入前中断：范围与原样本窗口必须保留，恢复后继续处理。
                scope["interrupted"] = True
                scope["updated_at"] = _now()
                return {
                    "ok": True,
                    "status": "interrupted",
                    "scope_id": scope["id"],
                    "commit_id": commit_id,
                    "requested_partitions": list(partitions),
                    "pending_partitions": list(scope["pending_partitions"]),
                    "published_partitions": [],
                    "blocked_partitions": [],
                    "actual_publish_range": [],
                    "sample_window_ids": dict(scope["sample_window_ids"]),
                    "message": "提交在分区写入前中断，原样本窗口与待处理分区已保留",
                }
            result = self._process_scope(scope)
            if result["ok"]:
                store.remember_submission(key, result)
            return result

    def resume_quality_commit(self, scope_id: int) -> dict[str, Any]:
        with store.gate_lock():
            scope = next(
                (row for row in store.rows(SCOPE_TABLE) if int(row["id"]) == scope_id),
                None,
            )
            if scope is None:
                return {"ok": False, "message": f"提交批次 {scope_id} 不存在"}
            if not scope.get("interrupted") and scope["status"] != "in_progress":
                return {
                    "ok": True,
                    "status": scope["status"],
                    "scope_id": scope["id"],
                    "commit_id": scope["commit_id"],
                    "requested_partitions": list(scope["requested_partitions"]),
                    "pending_partitions": list(scope["pending_partitions"]),
                    "published_partitions": list(scope["published_partitions"]),
                    "blocked_partitions": list(scope["blocked_partitions"]),
                    "actual_publish_range": list(scope["published_partitions"]),
                    "sample_window_ids": dict(scope["sample_window_ids"]),
                    "message": "该批次已处理完成，无需恢复",
                }
            scope["interrupted"] = False
            result = self._process_scope(scope)
            if result["ok"]:
                store.remember_submission(scope["scope_key"], result)
            return result

    def _process_scope(self, scope: dict[str, Any]) -> dict[str, Any]:
        active_map = {p: _active_verdict(p) for p in scope["requested_partitions"]}
        missing = [p for p, verdict in active_map.items() if verdict is None]
        if missing:
            scope["status"] = "failed"
            scope["updated_at"] = _now()
            return {
                "ok": False,
                "scope_id": scope["id"],
                "commit_id": scope["commit_id"],
                "message": f"分区缺少摄入质量判定数据：{'、'.join(missing)}",
                "missing_partitions": missing,
            }
        # 恢复时补齐首次中断前冻结的样本窗口，保证原窗口始终可追溯。
        for partition, verdict in active_map.items():
            scope["sample_window_ids"].setdefault(partition, verdict["window_id"])
            scope["frozen_samples"].setdefault(
                partition,
                {
                    "verdict_id": verdict["id"],
                    "window_id": verdict["window_id"],
                    "threshold": verdict["threshold"],
                    "valid_rows": verdict["valid_rows"],
                    "total_rows": verdict["total_rows"],
                },
            )
        write_count = 0
        items: list[dict[str, Any]] = []
        for partition in list(scope["pending_partitions"]):
            verdict = active_map[partition]
            existing = _published_for_commit(scope["commit_id"], partition)
            if existing is not None and (
                existing["scope_id"] == scope["id"] or existing["published"]
            ):
                # 同批次恢复时已处理过，或分区已在其他批次发布：绝不二次写入。
                # 历史阻断记录不属于本批次，落到下方按最新判定重新处理。
                self._mark_scope_partition(scope, partition, existing["published"])
                items.append(_record_view(existing))
                continue
            record = {
                "id": store.next_id(PUBLISH_TABLE),
                "commit_id": scope["commit_id"],
                "scope_id": scope["id"],
                "partition": partition,
                "verdict_id": verdict["id"],
                "published": bool(verdict["passed"]),
                "block_reason": None if verdict["passed"] else verdict["decision_reason"],
                "threshold": verdict["threshold"],
                "rule": verdict["rule"],
                "valid_rows": verdict["valid_rows"],
                "total_rows": verdict["total_rows"],
                "valid_rate": verdict["valid_rate"],
                "sample_window_id": verdict["window_id"],
                "sample_snapshot": copy.deepcopy(verdict["sample_snapshot"]),
                "source_refs": list(verdict["source_refs"]),
                "write_count": 1,
                "created_at": _now(),
            }
            store.rows(PUBLISH_TABLE).append(record)
            write_count += 1
            if record["published"]:
                _add_review_todo(
                    todo_type="published",
                    partition=partition,
                    commit_id=scope["commit_id"],
                    scope_id=scope["id"],
                    verdict_id=verdict["id"],
                    sample_window_id=verdict["window_id"],
                    threshold=verdict["threshold"],
                    valid_rows=verdict["valid_rows"],
                    total_rows=verdict["total_rows"],
                    valid_rate=verdict["valid_rate"],
                    passed=True,
                    source_refs=verdict["source_refs"],
                    message=f"分区 {partition} 质量判定合格，已随版本 {scope['commit_id']} 发布",
                )
            else:
                _add_review_todo(
                    todo_type="blocked",
                    partition=partition,
                    commit_id=scope["commit_id"],
                    scope_id=scope["id"],
                    verdict_id=verdict["id"],
                    sample_window_id=verdict["window_id"],
                    threshold=verdict["threshold"],
                    valid_rows=verdict["valid_rows"],
                    total_rows=verdict["total_rows"],
                    valid_rate=verdict["valid_rate"],
                    passed=False,
                    source_refs=verdict["source_refs"],
                    message=(
                        f"分区 {partition} 有效率 {verdict['valid_rate']} 低于阈值 "
                        f"{verdict['threshold']}，发布被阻断，留在待处理"
                    ),
                )
            self._mark_scope_partition(scope, partition, record["published"])
            items.append(_record_view(record))
        if scope["pending_partitions"]:
            scope["status"] = "interrupted"
            scope["interrupted"] = True
        elif scope["blocked_partitions"] and scope["published_partitions"]:
            scope["status"] = "partial"
            scope["interrupted"] = False
        elif scope["blocked_partitions"]:
            scope["status"] = "blocked"
            scope["interrupted"] = False
        else:
            scope["status"] = "published"
            scope["interrupted"] = False
        scope["updated_at"] = _now()
        return {
            "ok": True,
            "status": scope["status"],
            "scope_id": scope["id"],
            "scope_key": scope["scope_key"],
            "commit_id": scope["commit_id"],
            "requested_partitions": list(scope["requested_partitions"]),
            "pending_partitions": list(scope["pending_partitions"]),
            "published_partitions": list(scope["published_partitions"]),
            "blocked_partitions": list(scope["blocked_partitions"]),
            "actual_publish_range": list(scope["published_partitions"]),
            "sample_window_ids": dict(scope["sample_window_ids"]),
            "write_count": write_count,
            "items": items,
            "message": self._scope_message(scope),
        }

    @staticmethod
    def _mark_scope_partition(
        scope: dict[str, Any], partition: str, published: bool
    ) -> None:
        if partition in scope["pending_partitions"]:
            scope["pending_partitions"].remove(partition)
        target = scope["published_partitions"] if published else scope["blocked_partitions"]
        if partition not in target:
            target.append(partition)
        other = scope["blocked_partitions"] if published else scope["published_partitions"]
        if partition in other:
            other.remove(partition)

    @staticmethod
    def _scope_message(scope: dict[str, Any]) -> str:
        if scope["status"] == "published":
            return f"全部 {len(scope['published_partitions'])} 个分区发布成功"
        if scope["status"] == "partial":
            return (
                f"分区 {','.join(scope['published_partitions'])} 已发布；"
                f"分区 {','.join(scope['blocked_partitions'])} 未达阈值被阻断"
            )
        if scope["status"] == "blocked":
            return f"分区 {','.join(scope['blocked_partitions'])} 全部被阻断，未产生发布范围"
        return "批次仍有待处理分区"

    # ------------------------------------------------------------------
    # 版本详情：每个已提交分区可追溯采用的样本和阈值
    # ------------------------------------------------------------------
    def get_commit_detail(self, commit_id: int) -> dict[str, Any]:
        if store.find("delta_commit", commit_id) is None:
            return {"ok": False, "message": f"提交版本 {commit_id} 不存在"}
        with store.gate_lock():
            records = [
                _record_view(row)
                for row in store.rows(PUBLISH_TABLE)
                if row["commit_id"] == commit_id
            ]
            scopes = [
                _scope_view(row)
                for row in store.rows(SCOPE_TABLE)
                if row["commit_id"] == commit_id
            ]
            requested: list[str] = []
            for scope in scopes:
                for partition in scope["requested_partitions"]:
                    if partition not in requested:
                        requested.append(partition)
            # 同一分区重试会留下多条记录，发布范围以每个分区的最新判定记录为准；
            # 历史阻断记录仍保留在 partitions 明细中用于复核追溯。
            latest_by_partition: dict[str, dict[str, Any]] = {}
            for record in records:
                latest_by_partition[record["partition"]] = record
            return {
                "ok": True,
                "commit_id": commit_id,
                "requested_partitions": requested,
                "actual_publish_range": [
                    partition
                    for partition, record in latest_by_partition.items()
                    if record["published"]
                ],
                "blocked_partitions": [
                    partition
                    for partition, record in latest_by_partition.items()
                    if not record["published"]
                ],
                "scopes": scopes,
                "partitions": records,
            }

    # ------------------------------------------------------------------
    # 质量复核待办：已发布 / 新证据 / 被阻断 三类可区分
    # ------------------------------------------------------------------
    def list_review_todos(
        self,
        *,
        todo_type: str | None = None,
        status: str | None = None,
        partition: str | None = None,
    ) -> dict[str, Any]:
        with store.gate_lock():
            rows = store.rows(TODO_TABLE)
            if todo_type is not None:
                rows = [row for row in rows if row["todo_type"] == todo_type]
            if status is not None:
                rows = [row for row in rows if row["status"] == status]
            if partition is not None:
                rows = [row for row in rows if row["partition"] == partition]
            items = [copy.deepcopy(row) for row in rows]
            summary: dict[str, dict[str, int]] = {}
            for kind in ("published", "blocked", "new_evidence"):
                kind_rows = [row for row in store.rows(TODO_TABLE) if row["todo_type"] == kind]
                summary[kind] = {
                    "open": sum(1 for row in kind_rows if row["status"] == "open"),
                    "resolved": sum(1 for row in kind_rows if row["status"] == "resolved"),
                    "total": len(kind_rows),
                }
            return {
                "ok": True,
                "items": items,
                "summary": summary,
                "labels": {
                    "published": "已发布分区",
                    "blocked": "被阻断范围",
                    "new_evidence": "新证据",
                },
            }

    def resolve_review_todo(self, todo_id: int, resolution: str = "人工复核确认") -> dict[str, Any]:
        with store.gate_lock():
            todo = next(
                (row for row in store.rows(TODO_TABLE) if int(row["id"]) == todo_id),
                None,
            )
            if todo is None:
                return {"ok": False, "message": f"质量复核待办 {todo_id} 不存在"}
            todo["status"] = "resolved"
            todo["resolved_at"] = _now()
            todo["resolution"] = resolution
            return {"ok": True, "item": copy.deepcopy(todo)}

    # ------------------------------------------------------------------
    # 重试被阻断的分区：更正后只补写阻断分区，已发布分区不再写入
    # ------------------------------------------------------------------
    def retry_commit_partitions(
        self,
        commit_id: int,
        partitions: list[str],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        partitions = [str(p) for p in partitions]
        if store.find("delta_commit", commit_id) is None:
            return {"ok": False, "message": f"提交版本 {commit_id} 不存在"}
        # 只有真正发布过的分区才跳过；被阻断的分区仍属于可重试范围。
        already_published = [
            p
            for p in partitions
            if (record := _published_for_commit(commit_id, p)) is not None
            and record["published"]
        ]
        retry_partitions = [p for p in partitions if p not in already_published]
        if not retry_partitions:
            return {
                "ok": True,
                "status": "published",
                "commit_id": commit_id,
                "actual_publish_range": list(already_published),
                "write_count": 0,
                "message": "所选分区均已发布，未发生重复写入",
            }
        result = self.submit_quality_commit(
            commit_id,
            retry_partitions,
            idempotency_key=idempotency_key,
        )
        if already_published:
            result.setdefault("skipped_already_published", []).extend(already_published)
        return result


quality_gate_service = QualityGateService()
