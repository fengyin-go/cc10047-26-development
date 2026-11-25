"""摄入质量门禁端到端测试。

覆盖验收场景：80% 门槛下 p1=9/10 合格、p2=1/2 被阻断；更正 p2 失败行后重试；
核对版本详情、质量待办中的样本依据、实际发布范围以及 p1 不重复写入；
并发/重复提交只写一次；中断恢复保留原样本窗口与待处理分区；迟到更正与原判定冲突
时可区分已发布分区、新证据和被阻断范围。
"""
from __future__ import annotations

import sys
import threading

sys.path.insert(0, '.')

from fastapi.testclient import TestClient

from app.main import app
from app.services.quality_gate import quality_gate_service
from app.store import store

client = TestClient(app)

THRESHOLD = 0.8

# 种子数据里 delta_commit 只有 1~3 号，测试需要更多独立提交版本避免跨用例污染。
for _commit_id in (4, 5):
    if store.find("delta_commit", _commit_id) is None:
        store.rows("delta_commit").append(
            {
                "id": _commit_id,
                "status": "提交中",
                "pending": True,
                "abnormal": False,
                "提交标识": f"门禁测试提交{_commit_id}",
                "目标湖表": "湖表-门禁",
                "提交类型": "质量门禁提交",
                "新增行数": 0,
                "更新行数": 0,
                "删除行数": 0,
                "提交水位": f"wm-{_commit_id}",
                "提交时间": "2026-11-25",
            }
        )


def _evaluate(p1_valid=9, p1_total=10, p2_valid=1, p2_total=2, tag="e2e"):
    return quality_gate_service.evaluate_partition_samples(
        check_id=99,
        samples=[
            {
                "partition": f"p1-{tag}",
                "valid_rows": p1_valid,
                "total_rows": p1_total,
                "threshold": THRESHOLD,
                "rule": "ratio",
                "window_id": f"win-p1-{tag}",
                "source_refs": [f"src-p1-{tag}-a", f"src-p1-{tag}-b"],
            },
            {
                "partition": f"p2-{tag}",
                "valid_rows": p2_valid,
                "total_rows": p2_total,
                "threshold": THRESHOLD,
                "rule": "ratio",
                "window_id": f"win-p2-{tag}",
                "source_refs": [f"src-p2-{tag}-a"],
            },
        ],
    )


class TestPartitionVerdict:
    def test_partitions_judged_independently(self):
        result = _evaluate(tag="indep")
        assert result["ok"] is True
        by_partition = {v["partition"]: v for v in result["verdicts"]}
        p1 = by_partition["p1-indep"]
        p2 = by_partition["p2-indep"]
        # p1：9/10 = 90%，达到 80% 门槛
        assert p1["valid_rows"] == 9 and p1["total_rows"] == 10
        assert p1["valid_rate"] == 0.9 and p1["passed"] is True
        # p2：1/2 = 50%，未达 80% 门槛
        assert p2["valid_rows"] == 1 and p2["total_rows"] == 2
        assert p2["valid_rate"] == 0.5 and p2["passed"] is False
        # 总体 10/12 ≈ 83.3% 达标，但 p2 仍必须被阻断，不能被 p1 抵掉
        assert result["publishable_partitions"] == ["p1-indep"]
        assert p1["source_refs"] == ["src-p1-indep-a", "src-p1-indep-b"]
        assert p2["source_refs"] == ["src-p2-indep-a"]

    def test_re_evaluate_same_window_is_idempotent(self):
        _evaluate(tag="idem-verdict")
        before = len(store.rows("quality_verdicts"))
        again = _evaluate(tag="idem-verdict")
        after = len(store.rows("quality_verdicts"))
        assert again["ok"] is True
        assert before == after  # 同窗口同样本不产生新判定行

    def test_correction_keeps_history_and_conflicts(self):
        _evaluate(tag="history")
        p2 = next(
            v for v in quality_gate_service.list_verdicts("p2-history")["items"]
            if v["status"] == "active"
        )
        corrected = quality_gate_service.correct_partition_sample(
            "p2-history", 2, 2, note="迟到更正补齐失败行"
        )
        assert corrected["ok"] is True
        new_verdict = corrected["verdict"]
        assert new_verdict["version"] == 2
        assert new_verdict["passed"] is True
        # 原判定作为历史保留，样本窗口不变
        assert new_verdict["window_id"] == "win-p2-history"
        assert len(new_verdict["history"]) == 1
        assert new_verdict["history"][0]["valid_rows"] == 1
        assert corrected["conflict"]["conflict"] is True
        assert "valid_rows" in corrected["conflict"]["changed_fields"]


class TestCommitPublishFlow:
    def test_full_acceptance_flow(self):
        tag = "flow"
        p1, p2 = f"p1-{tag}", f"p2-{tag}"
        _evaluate(tag=tag)

        # 提交一批：只发布 p1，p2 被阻断
        submitted = quality_gate_service.submit_quality_commit(
            1, [p1, p2], idempotency_key=f"key-{tag}-1"
        )
        assert submitted["ok"] is True
        assert submitted["actual_publish_range"] == [p1]
        assert submitted["blocked_partitions"] == [p2]
        assert submitted["write_count"] == 2  # 每个分区一条发布/阻断记录
        assert submitted["sample_window_ids"][p1] == f"win-{p1}"
        assert submitted["sample_window_ids"][p2] == f"win-{p2}"

        # 版本详情：每个已提交分区都能查到采用的样本和阈值
        detail = quality_gate_service.get_commit_detail(1)
        assert detail["actual_publish_range"] == [p1]
        assert detail["blocked_partitions"] == [p2]
        detail_by_partition = {row["partition"]: row for row in detail["partitions"]}
        assert detail_by_partition[p1]["threshold"] == THRESHOLD
        assert detail_by_partition[p1]["sample_snapshot"]["valid_rows"] == 9
        assert detail_by_partition[p1]["sample_snapshot"]["total_rows"] == 10
        assert detail_by_partition[p2]["sample_snapshot"]["valid_rows"] == 1
        assert detail_by_partition[p2]["published"] is False

        # 质量复核待办：已发布分区与被阻断范围可区分
        todos = quality_gate_service.list_review_todos()
        published = [t for t in todos["items"] if t["todo_type"] == "published"]
        blocked = [t for t in todos["items"] if t["todo_type"] == "blocked"]
        assert any(t["partition"] == p1 for t in published)
        assert any(t["partition"] == p2 and t["passed"] is False for t in blocked)
        assert todos["summary"]["published"]["total"] >= 1
        assert todos["summary"]["blocked"]["total"] >= 1

        # 迟到更正：p2 失败行更正为 2/2，原判定与当前样本冲突，登记新证据
        corrected = quality_gate_service.correct_partition_sample(
            p2, 2, 2, note="迟到更正补齐失败行"
        )
        assert corrected["conflict"]["conflict"] is True
        refreshed = quality_gate_service.list_review_todos()
        evidence = [
            t for t in refreshed["items"]
            if t["todo_type"] == "new_evidence" and t["partition"] == p2
        ]
        assert evidence and evidence[-1]["status"] == "open"
        assert evidence[-1]["conflict"]["frozen_sample"]["valid_rows"] == 1
        assert evidence[-1]["conflict"]["current_sample"]["valid_rows"] == 2

        # 重试 p2：补写 p2；p1 已发布，绝不二次写入
        retry = quality_gate_service.retry_commit_partitions(
            1, [p1, p2], idempotency_key=f"key-{tag}-retry"
        )
        assert retry["ok"] is True
        assert retry["actual_publish_range"] == [p2]
        assert retry["write_count"] == 1
        assert retry["skipped_already_published"] == [p1]

        # 刷新版本详情：实际发布范围变为 p1 + p2，且 p1 仍只有一条写入记录
        final_detail = quality_gate_service.get_commit_detail(1)
        assert sorted(final_detail["actual_publish_range"]) == sorted([p1, p2])
        p1_records = [r for r in final_detail["partitions"] if r["partition"] == p1]
        assert len(p1_records) == 1
        assert p1_records[0]["write_count"] == 1

        # 待办中三类信息仍然可区分
        final_todos = quality_gate_service.list_review_todos()
        kinds = {t["todo_type"] for t in final_todos["items"] if t["partition"] in (p1, p2)}
        assert {"published", "blocked", "new_evidence"} <= kinds

    def test_concurrent_submit_same_scope_writes_once(self):
        tag = "concurrent"
        p1, p2 = f"p1-{tag}", f"p2-{tag}"
        _evaluate(tag=tag)
        results: list[dict] = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                results.append(
                    quality_gate_service.submit_quality_commit(
                        2, [p1, p2], idempotency_key=f"concurrent-key-{tag}"
                    )
                )
            except Exception as exc:  # pragma: no cover - 测试失败时暴露异常
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        # 5 个并发请求只有一次真实写入，其余为幂等重放
        real_writes = [r for r in results if not r.get("idempotent_replay")]
        replays = [r for r in results if r.get("idempotent_replay")]
        assert len(real_writes) == 1 and len(replays) == 4
        publish_rows = [
            row for row in store.rows("commit_publish_records")
            if row["commit_id"] == 2
        ]
        assert len(publish_rows) == 2
        assert {row["partition"] for row in publish_rows} == {p1, p2}

    def test_interrupted_resume_keeps_window_and_pending(self):
        tag = "resume"
        p1, p2 = f"p1-{tag}", f"p2-{tag}"
        _evaluate(tag=tag)
        interrupted = quality_gate_service.submit_quality_commit(
            3, [p1, p2], idempotency_key=f"resume-key-{tag}", fail_after_scope=True
        )
        assert interrupted["status"] == "interrupted"
        assert interrupted["pending_partitions"] == [p1, p2]
        assert interrupted["sample_window_ids"] == {
            p1: f"win-{p1}",
            p2: f"win-{p2}",
        }
        # 中断期间没有任何分区写入
        assert not [
            row for row in store.rows("commit_publish_records")
            if row["commit_id"] == 3
        ]
        resumed = quality_gate_service.resume_quality_commit(interrupted["scope_id"])
        assert resumed["ok"] is True
        assert resumed["actual_publish_range"] == [p1]
        assert resumed["blocked_partitions"] == [p2]
        # 恢复后仍引用原样本窗口
        detail = quality_gate_service.get_commit_detail(3)
        assert detail["scopes"][0]["sample_window_ids"][p1] == f"win-{p1}"
        assert all(row["sample_window_id"] == f"win-{row['partition']}"
                   for row in detail["partitions"])

    def test_retry_without_correction_stays_blocked(self):
        tag = "retry-blocked"
        p2 = f"p2-{tag}"
        _evaluate(tag=tag)
        first = quality_gate_service.submit_quality_commit(
            3, [p2], idempotency_key=f"rb-key-{tag}"
        )
        assert first["actual_publish_range"] == []
        # 未更正直接重试：仍按当前判定被阻断，不产生任何实际发布写入
        again = quality_gate_service.submit_quality_commit(
            3, [p2], idempotency_key=f"rb-key-2-{tag}"
        )
        assert again["blocked_partitions"] == [p2]
        assert again["actual_publish_range"] == []
        records = [
            row for row in store.rows("commit_publish_records")
            if row["commit_id"] == 3 and row["partition"] == p2
        ]
        # 两次尝试都留有阻断审计记录，但没有一条是成功发布
        assert all(row["published"] is False for row in records)
        detail = quality_gate_service.get_commit_detail(3)
        assert p2 in detail["blocked_partitions"]
        assert p2 not in detail["actual_publish_range"]


class TestQualityGateApi:
    def test_http_flow(self):
        tag = "http"
        p1, p2 = f"p1-{tag}", f"p2-{tag}"
        eval_resp = client.post(
            "/api/quality-gate/verdicts",
            json={
                "check_id": 7,
                "samples": [
                    {"partition": p1, "valid_rows": 9, "total_rows": 10,
                     "threshold": 0.8, "window_id": f"win-{p1}",
                     "source_refs": ["s1", "s2"]},
                    {"partition": p2, "valid_rows": 1, "total_rows": 2,
                     "threshold": 0.8, "window_id": f"win-{p2}",
                     "source_refs": ["s3"]},
                ],
            },
        )
        assert eval_resp.status_code == 200
        assert eval_resp.json()["publishable_partitions"] == [p1]

        submit_resp = client.post(
            "/api/quality-gate/commits/4/submit",
            json={"partitions": [p1, p2], "idempotency_key": f"http-submit-{tag}"},
        )
        assert submit_resp.status_code == 200
        assert submit_resp.json()["actual_publish_range"] == [p1]

        detail_resp = client.get("/api/quality-gate/commits/4/detail")
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert detail["blocked_partitions"] == [p2]
        assert all("sample_snapshot" in row and "threshold" in row
                   for row in detail["partitions"])

        todo_resp = client.get("/api/quality-gate/review-todos")
        assert todo_resp.status_code == 200
        payload = todo_resp.json()
        assert set(payload["labels"]) == {"published", "blocked", "new_evidence"}

        correct_resp = client.post(
            "/api/quality-gate/verdicts/correct",
            json={"partition": p2, "valid_rows": 2, "total_rows": 2, "note": "迟到更正"},
        )
        assert correct_resp.status_code == 200
        assert correct_resp.json()["conflict"]["conflict"] is True

        retry_resp = client.post(
            "/api/quality-gate/commits/4/retry",
            json={"partitions": [p1, p2], "idempotency_key": f"http-retry-{tag}"},
        )
        assert retry_resp.status_code == 200
        assert retry_resp.json()["actual_publish_range"] == [p2]
        assert retry_resp.json()["skipped_already_published"] == [p1]

    def test_submit_unknown_commit_rejected(self):
        resp = client.post(
            "/api/quality-gate/commits/9999/submit",
            json={"partitions": ["p-ghost"]},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    def test_submit_without_verdict_rejected(self):
        resp = client.post(
            "/api/quality-gate/commits/4/submit",
            json={"partitions": ["p-never-evaluated"], "idempotency_key": "ghost"},
        )
        assert resp.json()["ok"] is False
        assert "p-never-evaluated" in resp.json()["missing_partitions"]
