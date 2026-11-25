"""领域服务单元测试：验证所有18个domain_xxx方法的输出结构。"""
from __future__ import annotations

import sys
sys.path.insert(0, '.')

from app.services import service_for


class TestStreamSource:
    def test_detect_schema(self):
        svc = service_for("stream_source")
        result = svc.domain_detect_schema(1)
        assert result["ok"] is True
        assert "schema" in result
        assert "detected_fields" in result


class TestStreamIngest:
    def test_ingest_batch(self):
        svc = service_for("stream_ingest")
        result = svc.domain_ingest_batch(1, batch_size=2000)
        assert result["ok"] is True
        assert result["ingested_rows"] == 2000


class TestDeltaCommit:
    def test_commit(self):
        svc = service_for("delta_commit")
        result = svc.domain_commit(1)
        assert result["ok"] is True
        assert "version" in result

    def test_ingest_delta(self):
        svc = service_for("delta_commit")
        result = svc.domain_ingest_delta(1, delta_records=300)
        assert result["ok"] is True
        assert result["delta_records"] == 300


class TestLakeTable:
    def test_define_table(self):
        svc = service_for("lake_table")
        result = svc.domain_define_table(1)
        assert result["ok"] is True
        assert "ddl" in result


class TestPartitionMerge:
    def test_merge_partitions(self):
        svc = service_for("partition_merge")
        result = svc.domain_merge_partitions(1)
        assert result["ok"] is True
        assert "merged_files" in result


class TestCompactionTask:
    def test_plan_compaction(self):
        svc = service_for("compaction_task")
        result = svc.domain_plan_compaction(1)
        assert result["ok"] is True

    def test_compact_table(self):
        svc = service_for("compaction_task")
        result = svc.domain_compact_table(1)
        assert result["ok"] is True
        assert "compression_ratio" in result


class TestStreamSchema:
    def test_evolve_schema(self):
        svc = service_for("stream_schema")
        result = svc.domain_evolve_schema(1)
        assert result["ok"] is True
        assert "evolved_version" in result


class TestBackfillPipeline:
    def test_backfill(self):
        svc = service_for("backfill_pipeline")
        result = svc.domain_backfill(1)
        assert result["ok"] is True
        assert "completed_rows" in result


class TestBatchMerge:
    def test_merge_batch_stream(self):
        svc = service_for("batch_merge")
        result = svc.domain_merge_batch_stream(1)
        assert result["ok"] is True
        assert "reconciled_total" in result


class TestSnapshotMgr:
    def test_create_snapshot(self):
        svc = service_for("snapshot_mgr")
        result = svc.domain_create_snapshot(1)
        assert result["ok"] is True
        assert "row_count" in result


class TestVacuumJob:
    def test_clean_history(self):
        svc = service_for("vacuum_job")
        result = svc.domain_clean_history(1)
        assert result["ok"] is True
        assert "files_cleaned" in result


class TestRetentionPolicy:
    def test_check_retention(self):
        svc = service_for("retention_policy")
        result = svc.domain_check_retention(1)
        assert result["ok"] is True
        assert "should_clean" in result


class TestLakeLineage:
    def test_trace_lineage(self):
        svc = service_for("lake_lineage")
        result = svc.domain_trace_lineage(1)
        assert result["ok"] is True
        assert "upstream_tables" in result


class TestLakeAudit:
    def test_run_audit(self):
        svc = service_for("lake_audit")
        result = svc.domain_run_audit(1)
        assert result["ok"] is True
        assert "recommendations" in result


class TestTableCatalog:
    def test_sync_catalog(self):
        svc = service_for("table_catalog")
        result = svc.domain_sync_catalog(1)
        assert result["ok"] is True
        assert "synced_tables" in result


class TestStreamCheckpoint:
    def test_save_checkpoint(self):
        svc = service_for("stream_checkpoint")
        result = svc.domain_save_checkpoint(1)
        assert result["ok"] is True
        assert "checksum" in result


class TestStreamQualityGate:
    def test_evaluate_partition_samples(self):
        svc = service_for("stream_quality")
        result = svc.domain_evaluate_partition_samples(
            1,
            [
                {"partition": "svc-p1", "valid_rows": 9, "total_rows": 10,
                 "threshold": 0.8, "window_id": "win-svc-p1",
                 "source_refs": ["src-a"]},
                {"partition": "svc-p2", "valid_rows": 1, "total_rows": 2,
                 "threshold": 0.8, "window_id": "win-svc-p2",
                 "source_refs": ["src-b"]},
            ],
        )
        assert result["ok"] is True
        assert result["publishable_partitions"] == ["svc-p1"]
        entry = svc.get_entry(1)
        assert "可发布分区 1/2" in entry["上次结果"]

    def test_evaluate_unknown_check_rejected(self):
        svc = service_for("stream_quality")
        result = svc.domain_evaluate_partition_samples(99999, [])
        assert result["ok"] is False


class TestDomainSnapshot:
    def test_all_services_have_domain_snapshot(self):
        """确保所有18个服务都有 domain_snapshot 方法。"""
        modules = [
            "stream_source", "stream_ingest", "delta_commit", "lake_table",
            "partition_merge", "compaction_task", "stream_schema", "backfill_pipeline",
            "batch_merge", "snapshot_mgr", "vacuum_job", "retention_policy",
            "lake_lineage", "lake_audit", "table_catalog", "stream_checkpoint",
        ]
        for module in modules:
            svc = service_for(module)
            assert svc is not None, f"服务 {module} 未注册"
            snapshot = svc.domain_snapshot()
            assert "module" in snapshot
            assert snapshot["module"] == module