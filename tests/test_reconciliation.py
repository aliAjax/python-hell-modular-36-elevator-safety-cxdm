import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied
from src.platform_sink import InMemoryPlatformSink, ReportError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.service = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.duty_a = Actor("duty-a", "dispatcher")
        self.duty_b = Actor("duty-b", "dispatcher")

    def tearDown(self):
        self.tmp.cleanup()

    def _equipment(self, asset_no="DT-001"):
        return self.service.create(self.admin, "equipment", {
            "asset_no": asset_no, "equipment_type": "elevator",
            "location": "Tower A", "inspection_interval_days": 365,
        })

    def _rescue_chain(self, equipment, alarm_at="2026-10-03T08:00:00Z",
                      arrived_at=None, completed_at=None):
        alarm = self.service.create(self.admin, "alarm", {
            "equipment_id": equipment["id"], "code": "TRAP", "occurred_at": alarm_at,
        })
        self.service.transition(self.admin, alarm["id"], "dispatch", {"team": "Alpha"})
        job = self.service.create(self.admin, "rescue_job", {
            "alarm_id": alarm["id"], "dedupe_key": "job-" + alarm["id"], "team": "Alpha",
        })
        if arrived_at:
            self.service.transition(self.admin, job["id"], "arrive", {"arrived_at": arrived_at})
            job = self.service.get(job["id"])
        if completed_at:
            self.service.transition(self.admin, job["id"], "complete", {
                "outcome": "freed", "completed_at": completed_at,
            })
            job = self.service.get(job["id"])
        return alarm, job

    def _import(self, actor=None, **overrides):
        event = {
            "event_ref": "EVT-1", "asset_no": "DT-001",
            "alarm_at": "2026-10-03T08:00:30Z",
        }
        event.update(overrides)
        items = self.service.import_platform_events(actor or self.duty_a, [event])
        return items[0]

    def test_claim_matches_local_alarm_and_rescue_times(self):
        equipment = self._equipment()
        self._rescue_chain(equipment, arrived_at="2026-10-03T08:07:00Z",
                           completed_at="2026-10-03T08:25:00Z")
        event = self._import()
        claim = self.service.claim_event(self.duty_a, event["id"])

        self.assertEqual(claim["status"], "matched")
        self.assertEqual(claim["data"]["claimed_by"], "duty-a")
        self.assertEqual(claim["data"]["local_arrived_at"], "2026-10-03T08:07:00Z")
        self.assertEqual(claim["data"]["local_completed_at"], "2026-10-03T08:25:00Z")
        self.assertEqual(claim["data"]["alarm_time_delta_seconds"], 30)

    def test_claim_without_local_record_is_discrepancy(self):
        self._equipment()
        event = self._import()
        claim = self.service.claim_event(self.duty_a, event["id"])
        self.assertEqual(claim["status"], "discrepancy")
        self.assertIn("no_local_alarm", claim["data"]["reasons"])
        self.assertIsNone(claim["data"]["local_arrived_at"])

    def test_ongoing_rescue_without_arrival_time_is_discrepancy(self):
        equipment = self._equipment()
        self._rescue_chain(equipment)  # 已派发但未到场
        event = self._import()
        claim = self.service.claim_event(self.duty_a, event["id"])
        self.assertEqual(claim["status"], "discrepancy")
        self.assertIn("missing_arrival_time", claim["data"]["reasons"])

    def test_alarm_time_outside_window_is_discrepancy(self):
        equipment = self._equipment()
        self._rescue_chain(equipment, alarm_at="2026-10-03T06:00:00Z",
                           arrived_at="2026-10-03T06:05:00Z",
                           completed_at="2026-10-03T06:20:00Z")
        event = self._import()  # 08:00:30，相差两小时
        claim = self.service.claim_event(self.duty_a, event["id"])
        self.assertEqual(claim["status"], "discrepancy")
        self.assertIn("no_local_alarm", claim["data"]["reasons"])

    def test_concurrent_claims_first_wins(self):
        equipment = self._equipment()
        self._rescue_chain(equipment, arrived_at="2026-10-03T08:07:00Z",
                           completed_at="2026-10-03T08:25:00Z")
        event = self._import()
        winners = []
        losers = []

        def claim(actor):
            try:
                winners.append(self.service.claim_event(actor, event["id"]))
            except ConflictError as exc:
                losers.append(exc)

        threads = [threading.Thread(target=claim, args=(self.duty_a,)),
                   threading.Thread(target=claim, args=(self.duty_b,))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        self.assertIn("already claimed", str(losers[0]))
        stored = self.service.repository.get_claim_for_event(event["id"])
        self.assertEqual(stored["data"]["claimed_by"], winners[0]["data"]["claimed_by"])

    def test_non_duty_roles_are_rejected(self):
        equipment = self._equipment()
        event = self._import(actor=Actor("duty", "dispatcher"))
        viewer = Actor("someone", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.claim_event(viewer, event["id"])
        with self.assertRaises(PermissionDenied):
            self.service.import_platform_events(viewer, [
                {"event_ref": "X", "asset_no": "DT-001", "alarm_at": "2026-10-03T08:00:00Z"},
            ])

    def test_claim_recomputes_when_rescue_arrives_and_completes(self):
        equipment = self._equipment()
        alarm, job = self._rescue_chain(equipment)
        event = self._import()
        claim = self.service.claim_event(self.duty_a, event["id"])
        self.assertEqual(claim["status"], "discrepancy")

        self.service.transition(self.admin, job["id"], "arrive",
                                {"arrived_at": "2026-10-03T08:07:00Z"})
        claim = self.service.get(claim["id"])
        self.assertEqual(claim["status"], "discrepancy")
        self.assertEqual(claim["data"]["local_arrived_at"], "2026-10-03T08:07:00Z")
        self.assertEqual(claim["data"]["recompute_count"], 1)
        self.assertEqual(claim["data"]["claimed_by"], "duty-a")  # 认领人不变

        self.service.transition(self.admin, job["id"], "complete",
                                {"outcome": "freed", "completed_at": "2026-10-03T08:25:00Z"})
        claim = self.service.get(claim["id"])
        self.assertEqual(claim["status"], "matched")
        self.assertEqual(claim["data"]["local_completed_at"], "2026-10-03T08:25:00Z")
        self.assertEqual(claim["data"]["recompute_count"], 2)

    def test_equipment_suspend_invalidates_claim_tied_by_asset_no(self):
        equipment = self._equipment()
        self._rescue_chain(equipment, arrived_at="2026-10-03T08:07:00Z",
                           completed_at="2026-10-03T08:25:00Z")
        event = self._import()
        claim = self.service.claim_event(self.duty_a, event["id"])
        self.assertEqual(claim["status"], "matched")

        self.service.transition(self.admin, equipment["id"], "suspend", {})
        claim = self.service.get(claim["id"])
        self.assertEqual(claim["data"]["recompute_count"], 1)
        # 设备状态变更不改变匹配结果，但指纹变化触发了作废重算
        self.assertEqual(claim["status"], "matched")

    def test_duplicate_import_is_idempotent(self):
        self._equipment()
        payload = {"event_ref": "EVT-1", "asset_no": "DT-001",
                   "alarm_at": "2026-10-03T08:00:00Z"}
        first = self.service.import_platform_events(self.duty_a, [payload])
        second = self.service.import_platform_events(self.duty_a, [dict(payload)])
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(len(self.service.list("platform_event")), 1)

    def test_failed_report_retried_only_until_sent_and_survives_restart(self):
        equipment = self._equipment()
        self._rescue_chain(equipment, arrived_at="2026-10-03T08:07:00Z",
                           completed_at="2026-10-03T08:25:00Z")
        event = self._import()
        claim = self.service.claim_event(self.duty_a, event["id"])

        failing = InMemoryPlatformSink(fail_claim_ids={claim["id"]})
        first = self.service.flush_reports(failing)
        self.assertEqual(first[0]["status"], "pending")
        pending = self.service.list_outbox(status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["attempts"], 1)

        # 重启：新服务实例指向同一个库，认领项仍在；只重试没写进去的
        restarted = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        restarted.refresh_claims()
        ok_sink = InMemoryPlatformSink()
        result = restarted.flush_reports(ok_sink)
        self.assertEqual(result[0]["status"], "sent")
        self.assertEqual(len(ok_sink.received), 1)
        self.assertEqual(ok_sink.received[0]["local_arrived_at"], "2026-10-03T08:07:00Z")

        # 已确认 sent 的项不再重发
        again = restarted.flush_reports(InMemoryPlatformSink())
        self.assertEqual(again, [])

    def test_claim_recomputes_when_local_records_are_created_later(self):
        equipment = self._equipment()
        event = self._import()
        claim = self.service.claim_event(self.duty_a, event["id"])
        self.assertEqual(claim["status"], "discrepancy")
        self.assertIn("no_local_alarm", claim["data"]["reasons"])

        # 认领之后本地才建报警并派发救援
        alarm = self.service.create(self.admin, "alarm", {
            "equipment_id": equipment["id"], "code": "TRAP",
            "occurred_at": "2026-10-03T08:00:20Z",
        })
        self.service.transition(self.admin, alarm["id"], "dispatch", {"team": "Alpha"})
        job = self.service.create(self.admin, "rescue_job", {
            "alarm_id": alarm["id"], "dedupe_key": "late-job", "team": "Alpha",
        })
        claim = self.service.get(claim["id"])
        self.assertEqual(claim["data"]["local_alarm_id"], alarm["id"])
        self.assertIn("missing_arrival_time", claim["data"]["reasons"])
        self.assertGreaterEqual(claim["data"]["recompute_count"], 2)  # 报警 + 救援建创触发

        self.service.transition(self.admin, job["id"], "arrive",
                                {"arrived_at": "2026-10-03T08:06:00Z"})
        self.service.transition(self.admin, job["id"], "complete",
                                {"outcome": "freed", "completed_at": "2026-10-03T08:25:00Z"})
        claim = self.service.get(claim["id"])
        self.assertEqual(claim["status"], "matched")
        self.assertEqual(claim["data"]["local_arrived_at"], "2026-10-03T08:06:00Z")
        self.assertEqual(claim["data"]["local_completed_at"], "2026-10-03T08:25:00Z")

    def test_refresh_claims_picks_up_out_of_band_drift(self):
        equipment = self._equipment()
        alarm, job = self._rescue_chain(equipment)
        event = self._import()
        claim = self.service.claim_event(self.duty_a, event["id"])
        self.assertEqual(claim["status"], "discrepancy")

        # 模拟库外直接改本地救援数据（不经 transition）
        repo = self.service.repository
        job = repo.get_entity(job["id"])
        job["data"]["arrived_at"] = "2026-10-03T08:07:00Z"
        repo.update_entity(job["id"], job["version"], "on_site", job["data"])

        restarted = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        refreshed = restarted.refresh_claims()
        self.assertEqual(len(refreshed), 1)
        stored = restarted.get(claim["id"])
        self.assertEqual(stored["data"]["local_arrived_at"], "2026-10-03T08:07:00Z")


if __name__ == "__main__":
    unittest.main()
