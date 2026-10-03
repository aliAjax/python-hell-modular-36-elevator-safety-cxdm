import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied
from src.reconciliation import (
    ALARM_MATCH_TOLERANCE_SECONDS,
    REASON_ARRIVAL_MISMATCH,
    REASON_COMPLETION_MISMATCH,
    REASON_LOCAL_RESCUE_NOT_REPORTED,
    REASON_MISSING_ARRIVAL,
    REASON_MISSING_COMPLETION,
    REASON_NO_LOCAL_RESCUE,
    REASON_NO_MATCHING_ALARM_TIME,
    ReportSink,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.repo = SQLiteRepository(self.db_path)
        self.sink = ReportSink()  # 默认上报成功
        self.service = DomainService(self.repo, RuleEngine(), report_sink=self.sink)
        self.admin = Actor("admin", "admin")
        self.duty = Actor("duty", "duty")
        self.duty2 = Actor("duty2", "duty")

    def tearDown(self):
        self.tmp.cleanup()

    def _equipment(self, asset_no):
        return self.service.create(
            self.admin, "equipment",
            {"asset_no": asset_no, "equipment_type": "elevator", "location": "A", "inspection_interval_days": 365},
        )

    def _rescue(self, asset_no, alarm_at, code="DOOR-JAM", team="Alpha", arrived_at=None, completed_at=None, do_arrive=True):
        equipment = self._equipment(asset_no)
        alarm = self.service.create(
            self.admin, "alarm",
            {"equipment_id": equipment["id"], "code": code, "occurred_at": alarm_at},
        )
        self.service.transition(self.admin, alarm["id"], "dispatch", {"team": team})
        job = self.service.create(
            self.admin, "rescue_job",
            {"alarm_id": alarm["id"], "dedupe_key": asset_no + "-" + code, "team": team},
        )
        if do_arrive:
            self.service.transition(self.admin, job["id"], "arrive", {"arrived_at": arrived_at} if arrived_at else {})
            if completed_at is not None:
                self.service.transition(self.admin, job["id"], "complete", {"outcome": "ok", "completed_at": completed_at})
        return equipment, alarm, job

    def _push(self, events):
        return self.service.push_platform_events(self.admin, events)

    def _claim(self, asset_no):
        return self.service.claim_reconciliation(self.duty, asset_no)

    # ---- 匹配 ----

    def test_platform_event_attached_to_local_rescue_with_local_times(self):
        self._rescue("E-1", "2026-09-27T10:00:00Z", arrived_at="2026-09-27T10:05:00Z", completed_at="2026-09-27T10:20:00Z")
        self._push([{"platform_event_id": "PE-1", "asset_no": "E-1", "alarm_at": "2026-09-27T10:00:00Z"}])
        recon = self._claim("E-1")
        items = recon["data"]["conclusion"]["items"]
        self.assertEqual(recon["data"]["conclusion"]["summary"], {"total": 1, "matched": 1, "discrepancy": 0})
        item = items[0]
        self.assertEqual(item["status"], "matched")
        self.assertEqual(item["rescue_job_id"], self.service.list("rescue_job")[0]["id"])
        self.assertEqual(item["arrived_at"], "2026-09-27T10:05:00Z")  # 到场时间以本地为准
        self.assertEqual(item["completed_at"], "2026-09-27T10:20:00Z")

    def test_alarm_time_within_tolerance_attached(self):
        self._rescue("E-1", "2026-09-27T10:00:00Z", arrived_at="2026-09-27T10:05:00Z", completed_at="2026-09-27T10:20:00Z")
        # 平台报警时刻比本地晚 4 分钟，在容差内 -> 仍挂到同一任务
        self._push([{"platform_event_id": "PE-1", "asset_no": "E-1", "alarm_at": "2026-09-27T10:04:00Z"}])
        recon = self._claim("E-1")
        self.assertEqual(recon["data"]["conclusion"]["summary"]["matched"], 1)

    def test_alarm_time_beyond_tolerance_is_discrepancy(self):
        self._rescue("E-1", "2026-09-27T10:00:00Z", arrived_at="2026-09-27T10:05:00Z", completed_at="2026-09-27T10:20:00Z")
        # 平台报警时刻比本地晚 6 分钟，超出容差 -> 对不上，留差异
        self._push([{"platform_event_id": "PE-1", "asset_no": "E-1", "alarm_at": "2026-09-27T10:06:00Z"}])
        recon = self._claim("E-1")
        item = recon["data"]["conclusion"]["items"][0]
        self.assertEqual(item["status"], "discrepancy")
        self.assertIn(REASON_NO_MATCHING_ALARM_TIME, item["reasons"])
        self.assertIsNone(item["rescue_job_id"])

    # ---- 差异项 ----

    def test_platform_event_without_local_rescue_is_discrepancy(self):
        self._push([{"platform_event_id": "PE-1", "asset_no": "E-99", "alarm_at": "2026-09-27T10:00:00Z"}])
        recon = self._claim("E-99")
        item = recon["data"]["conclusion"]["items"][0]
        self.assertEqual(item["status"], "discrepancy")
        self.assertIn(REASON_NO_LOCAL_RESCUE, item["reasons"])

    def test_missing_arrival_and_completion_are_discrepancies(self):
        # 救援任务只派了，没到场也没完成
        self._rescue("E-1", "2026-09-27T10:00:00Z", do_arrive=False)
        self._push([{"platform_event_id": "PE-1", "asset_no": "E-1", "alarm_at": "2026-09-27T10:00:00Z"}])
        recon = self._claim("E-1")
        item = recon["data"]["conclusion"]["items"][0]
        self.assertEqual(item["status"], "discrepancy")
        self.assertIn(REASON_MISSING_ARRIVAL, item["reasons"])
        self.assertIn(REASON_MISSING_COMPLETION, item["reasons"])

    def test_missing_completion_only(self):
        # 到场了但没完成
        self._rescue("E-1", "2026-09-27T10:00:00Z", arrived_at="2026-09-27T10:05:00Z")
        self._push([{"platform_event_id": "PE-1", "asset_no": "E-1", "alarm_at": "2026-09-27T10:00:00Z"}])
        recon = self._claim("E-1")
        item = recon["data"]["conclusion"]["items"][0]
        self.assertIn(REASON_MISSING_COMPLETION, item["reasons"])
        self.assertNotIn(REASON_MISSING_ARRIVAL, item["reasons"])

    def test_platform_arrival_time_mismatch_is_discrepancy(self):
        self._rescue("E-1", "2026-09-27T10:00:00Z", arrived_at="2026-09-27T10:05:00Z", completed_at="2026-09-27T10:20:00Z")
        self._push([{
            "platform_event_id": "PE-1", "asset_no": "E-1", "alarm_at": "2026-09-27T10:00:00Z",
            "arrived_at": "2026-09-27T10:30:00Z",  # 平台到场时间与本地差 25 分钟
        }])
        recon = self._claim("E-1")
        item = recon["data"]["conclusion"]["items"][0]
        self.assertIn(REASON_ARRIVAL_MISMATCH, item["reasons"])
        self.assertNotIn(REASON_COMPLETION_MISMATCH, item["reasons"])

    def test_platform_completion_time_mismatch_is_discrepancy(self):
        self._rescue("E-1", "2026-09-27T10:00:00Z", arrived_at="2026-09-27T10:05:00Z", completed_at="2026-09-27T10:20:00Z")
        self._push([{
            "platform_event_id": "PE-1", "asset_no": "E-1", "alarm_at": "2026-09-27T10:00:00Z",
            "completed_at": "2026-09-27T11:00:00Z",  # 平台完成时间与本地差 40 分钟
        }])
        recon = self._claim("E-1")
        item = recon["data"]["conclusion"]["items"][0]
        self.assertIn(REASON_COMPLETION_MISMATCH, item["reasons"])

    def test_local_rescue_without_platform_event_is_discrepancy(self):
        self._rescue("E-1", "2026-09-27T10:00:00Z", arrived_at="2026-09-27T10:05:00Z", completed_at="2026-09-27T10:20:00Z")
        # 本地有救援，但平台清单没有
        recon = self._claim("E-1")
        item = recon["data"]["conclusion"]["items"][0]
        self.assertEqual(item["status"], "discrepancy")
        self.assertIn(REASON_LOCAL_RESCUE_NOT_REPORTED, item["reasons"])

    # ---- 认领并发 ----

    def test_two_duty_claim_same_elevator_first_wins_second_sees_claimed(self):
        self._push([{"platform_event_id": "PE-1", "asset_no": "E-1", "alarm_at": "2026-09-27T10:00:00Z"}])
        first = self._claim("E-1")
        self.assertEqual(first["data"]["claimed_by"], "duty")
        with self.assertRaises(ConflictError) as ctx:
            self.service.claim_reconciliation(self.duty2, "E-1")
        self.assertIn("already claimed", str(ctx.exception))
        self.assertIn("duty", str(ctx.exception))  # 后到者看到已被谁认领

    # ---- 权限 ----

    def test_non_duty_claim_is_rejected(self):
        for role in ("viewer", "admin", "inspector", "dispatcher", "maintenance"):
            with self.subTest(role=role):
                with self.assertRaises(PermissionDenied):
                    self.service.claim_reconciliation(Actor(role, role), "E-1")

    def test_non_duty_report_is_rejected(self):
        self._push([{"platform_event_id": "PE-1", "asset_no": "E-1", "alarm_at": "2026-09-27T10:00:00Z"}])
        recon = self._claim("E-1")
        with self.assertRaises(PermissionDenied):
            self.service.report_reconciliation(Actor("viewer", "viewer"), recon["id"])

    # ---- 变更作废重算 ----

    def test_local_change_recalculates_claimed_conclusion(self):
        equipment, _, _ = self._rescue("E-1", "2026-09-27T10:00:00Z", arrived_at="2026-09-27T10:05:00Z", completed_at="2026-09-27T10:20:00Z")
        self._push([{"platform_event_id": "PE-1", "asset_no": "E-1", "alarm_at": "2026-09-27T10:00:00Z"}])
        recon = self._claim("E-1")
        self.assertEqual(recon["data"]["conclusion"]["summary"]["matched"], 1)
        version_before = recon["version"]

        # 本地新增一条救援任务（平台清单里没有）
        alarm2 = self.service.create(
            self.admin, "alarm",
            {"equipment_id": equipment["id"], "code": "TRAP", "occurred_at": "2026-09-27T11:00:00Z"},
        )
        self.service.transition(self.admin, alarm2["id"], "dispatch", {"team": "Beta"})
        job2 = self.service.create(
            self.admin, "rescue_job",
            {"alarm_id": alarm2["id"], "dedupe_key": "E-1-TRAP", "team": "Beta"},
        )
        self.service.transition(self.admin, job2["id"], "arrive", {})
        self.service.transition(self.admin, job2["id"], "complete", {"outcome": "ok"})

        updated = self.service.get(recon["id"])
        # 已认领结论立即作废重算：出现一条“本地救援未上报”差异
        self.assertEqual(updated["data"]["conclusion"]["summary"]["discrepancy"], 1)
        self.assertEqual(updated["data"]["conclusion"]["summary"]["matched"], 1)
        self.assertGreater(updated["version"], version_before)
        actions = [a["action"] for a in self.service.audit_log(recon["id"])]
        self.assertIn("recalculate", actions)

    def test_equipment_status_change_recalculates(self):
        equipment, _, _ = self._rescue("E-1", "2026-09-27T10:00:00Z", arrived_at="2026-09-27T10:05:00Z", completed_at="2026-09-27T10:20:00Z")
        self._push([{"platform_event_id": "PE-1", "asset_no": "E-1", "alarm_at": "2026-09-27T10:00:00Z"}])
        recon = self._claim("E-1")
        self.service.transition(self.admin, equipment["id"], "suspend", {})
        actions = [a["action"] for a in self.service.audit_log(recon["id"])]
        self.assertIn("recalculate", actions)

    # ---- 上报失败重试与重启续跑 ----

    def test_report_failure_keeps_written_and_retries_failed_only(self):
        self._rescue("E-1", "2026-09-27T10:00:00Z", arrived_at="2026-09-27T10:05:00Z", completed_at="2026-09-27T10:20:00Z")
        self._push([
            {"platform_event_id": "PE-10", "asset_no": "E-1", "alarm_at": "2026-09-27T10:00:00Z"},
            {"platform_event_id": "PE-11", "asset_no": "E-1", "alarm_at": "2026-09-27T10:00:00Z"},
        ])
        recon = self._claim("E-1")
        # PE-10 上报失败，PE-11 成功
        self.service.report_sink = ReportSink(fail_keys={"PE-10"})
        first = self.service.report_reconciliation(self.duty, recon["id"])
        self.assertEqual(first["written"], 1)
        self.assertEqual(first["failed"], 1)
        statuses = {i["item_key"]: i["status"] for i in first["items"]}
        self.assertEqual(statuses["PE-10"], "failed")
        self.assertEqual(statuses["PE-11"], "written")
        pe10 = next(i for i in first["items"] if i["item_key"] == "PE-10")
        self.assertEqual(pe10["attempts"], 1)
        self.assertTrue(pe10["last_error"])

        # 重试：只重试没写进去的 PE-10，PE-11 已写保留不重试
        self.service.report_sink = ReportSink()
        second = self.service.report_reconciliation(self.duty, recon["id"])
        self.assertEqual(second["written"], 1)
        self.assertEqual(second["failed"], 0)
        self.assertEqual(second["skipped"], 1)  # PE-11 已写，跳过
        statuses = {i["item_key"]: i["status"] for i in second["items"]}
        self.assertEqual(statuses["PE-10"], "written")
        self.assertEqual(statuses["PE-11"], "written")
        pe10 = next(i for i in second["items"] if i["item_key"] == "PE-10")
        pe11 = next(i for i in second["items"] if i["item_key"] == "PE-11")
        self.assertEqual(pe10["attempts"], 1)  # 成功不增加 attempts
        self.assertEqual(pe11["attempts"], 0)  # 已写项从未被重试

    def test_recover_after_restart_continues_pending_reports(self):
        self._rescue("E-1", "2026-09-27T10:00:00Z", arrived_at="2026-09-27T10:05:00Z", completed_at="2026-09-27T10:20:00Z")
        self._push([{"platform_event_id": "PE-10", "asset_no": "E-1", "alarm_at": "2026-09-27T10:00:00Z"}])
        recon = self._claim("E-1")
        self.service.report_sink = ReportSink(fail_keys={"PE-10"})
        result = self.service.report_reconciliation(self.duty, recon["id"])
        self.assertEqual(result["failed"], 1)

        # 模拟重启：新服务实例、新上报器（平台恢复），数据库里的失败项继续处理
        restarted = DomainService(SQLiteRepository(self.db_path), RuleEngine(), report_sink=ReportSink())
        recovered = restarted.recover_reports()
        self.assertEqual(recovered["written"], 1)
        self.assertEqual(recovered["failed"], 0)
        items = restarted.list_report_items(self.duty, recon["id"])
        self.assertEqual(items[0]["status"], "written")

    # ---- 平台清单幂等 ----

    def test_platform_push_is_idempotent(self):
        self._push([{"platform_event_id": "PE-1", "asset_no": "E-1", "alarm_at": "2026-09-27T10:00:00Z"}])
        self._push([{"platform_event_id": "PE-1", "asset_no": "E-1", "alarm_at": "2026-09-27T10:00:00Z"}])
        events = self.service.list("platform_event")
        self.assertEqual(len(events), 1)

    def test_push_requires_fields(self):
        with self.assertRaises(Exception):
            self._push([{"platform_event_id": "PE-1", "asset_no": "E-1"}])  # 缺 alarm_at


if __name__ == "__main__":
    unittest.main()
