import hashlib
import re
import sqlite3
from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError, PermissionDenied, ValidationError
from .reconciliation import ReportSink, build_conclusion
from .repository import utcnow
from .rules import RuleEngine


def _slug(value):
    return re.sub(r"[^A-Za-z0-9]+", "_", str(value)).strip("_") or "x"


class DomainService:
    # 这些种类的实体会影响对账结论，变更后需要触发已认领结论重算
    _RECALC_KINDS = ("equipment", "alarm", "rescue_job", "platform_event")

    def __init__(self, repository, rules=None, report_sink=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        self.report_sink = report_sink or ReportSink()

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _require_role(self, actor, roles):
        allowed = (roles,) if isinstance(roles, str) else tuple(roles)
        if actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        self._after_local_change(kind, entity, reason=kind + "_created")
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        self._after_local_change(updated["kind"], updated, reason=updated["kind"] + "_changed")
        return updated

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            existing = self.repository.get_entity(entity_id)
            if existing:
                created.append(existing)
                continue
            payload = dict(raw)
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            entity = self.repository.create_entity(
                entity_id,
                "offline_record",
                self.rules.initial_status("offline_record", payload),
                payload,
                actor.user_id,
            )
            self.audit.record(entity_id, actor, "merge_offline", None, entity["status"], {"source_id": source_id, "record_id": record_id})
            created.append(entity)
        return created

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ---- 平台困人报警对账 ----

    def _equipment_by_asset_no(self, asset_no):
        rows = self.repository.find_entities("equipment", "asset_no", str(asset_no))
        return rows[0] if rows else None

    def _local_records_for_asset(self, asset_no):
        """汇总某设备编号下的本地救援记录（救援任务 -> 报警 -> 设备）。"""
        equipment = self._equipment_by_asset_no(asset_no)
        if not equipment:
            return []
        records = []
        alarms = self.repository.find_entities("alarm", "equipment_id", equipment["id"])
        for alarm in alarms:
            for job in self.repository.find_entities("rescue_job", "alarm_id", alarm["id"]):
                records.append(
                    {
                        "rescue_job_id": job["id"],
                        "alarm_id": alarm["id"],
                        "alarm_at": alarm["data"].get("occurred_at"),
                        "asset_no": asset_no,
                        "arrived_at": job["data"].get("arrived_at"),
                        "completed_at": job["data"].get("completed_at"),
                    }
                )
        return records

    def _build_conclusion(self, asset_no):
        events = self.repository.find_entities("platform_event", "asset_no", str(asset_no))
        records = self._local_records_for_asset(asset_no)
        return build_conclusion(asset_no, events, records)

    def _asset_no_for_entity(self, kind, entity):
        kind = self.rules.normalize_kind(kind)
        if kind in ("equipment", "platform_event"):
            return entity["data"].get("asset_no")
        if kind == "alarm":
            equipment = self.repository.get_entity(entity["data"].get("equipment_id"))
            return equipment["data"].get("asset_no") if equipment else None
        if kind == "rescue_job":
            alarm = self.repository.get_entity(entity["data"].get("alarm_id"))
            if not alarm:
                return None
            equipment = self.repository.get_entity(alarm["data"].get("equipment_id"))
            return equipment["data"].get("asset_no") if equipment else None
        return None

    def _after_local_change(self, kind, entity, reason):
        kind = self.rules.normalize_kind(kind)
        if kind not in self._RECALC_KINDS:
            return None
        asset_no = self._asset_no_for_entity(kind, entity)
        return self.recalculate_for_asset(asset_no, reason=reason)

    def recalculate_for_asset(self, asset_no, reason="local_change"):
        """本地报警/救援/设备一变，已认领结论立即作废重算。

        结论是派生数据，采用“最后写入生效”的覆盖式更新；若与并发重算撞版本则重读重试一次。
        """
        if not asset_no:
            return None
        asset_no = str(asset_no)
        recon = None
        conclusion = None
        for _ in range(2):
            active = [
                e
                for e in self.repository.find_entities("reconciliation", "asset_no", asset_no)
                if e["status"] == "active"
            ]
            if not active:
                return None
            recon = active[0]
            conclusion = self._build_conclusion(asset_no)
            data = dict(recon["data"])
            data["conclusion"] = conclusion
            try:
                updated = self.repository.update_entity(recon["id"], recon["version"], "active", data)
                break
            except ConflictError:
                continue
        else:
            return None
        self.audit.record(
            recon["id"],
            Actor("system", "system"),
            "recalculate",
            "active",
            "active",
            {"reason": reason, "summary": conclusion["summary"]},
        )
        self._sync_report_items(recon["id"], conclusion)
        return updated

    def _sync_report_items(self, reconciliation_id, conclusion):
        """根据最新结论同步出报表：已写且内容未变的保留，其余置为待写，移除的标记作废。"""
        keep = set()
        for item in conclusion["items"]:
            key = str(item["item_key"])
            keep.add(key)
            item_id = "report-" + reconciliation_id + "-" + _slug(key)
            existing = self.repository.get_report_item(item_id)
            if existing and existing["status"] == "written" and existing["payload"] == item:
                continue  # 已上报且结论未变：保留，不重试
            self.repository.upsert_report_item(item_id, reconciliation_id, key, item, "pending")
        self.repository.supersede_report_items(reconciliation_id, keep)

    def push_platform_events(self, actor, events):
        """平台每天推来困人报警清单（只交代有哪些事件）。幂等：同一平台事件编号不重复入账。"""
        self._require_role(actor, ("admin", "dispatcher"))
        if not isinstance(events, list):
            raise ValidationError("events must be a list")
        created = []
        for raw in events:
            if not isinstance(raw, dict):
                raise ValidationError("each platform event must be an object")
            platform_event_id = str(raw.get("platform_event_id", "")).strip()
            asset_no = str(raw.get("asset_no", "")).strip()
            alarm_at = raw.get("alarm_at")
            if not platform_event_id or not asset_no or not alarm_at:
                raise ValidationError("platform_event_id, asset_no, alarm_at are required")
            existing = self.repository.find_entities("platform_event", "platform_event_id", platform_event_id)
            if existing:
                created.append(existing[0])
                continue
            digest = hashlib.sha256(platform_event_id.encode("utf-8")).hexdigest()[:16]
            entity_id = "platform-" + digest
            data = {
                "platform_event_id": platform_event_id,
                "asset_no": asset_no,
                "alarm_at": alarm_at,
            }
            for opt in ("arrived_at", "completed_at", "trapped_count", "location"):
                if opt in raw:
                    data[opt] = raw[opt]
            try:
                entity = self.repository.create_entity(entity_id, "platform_event", "pending", data, actor.user_id)
            except sqlite3.IntegrityError:
                found = self.repository.find_entities("platform_event", "platform_event_id", platform_event_id)
                if found:
                    created.append(found[0])
                continue
            self.audit.record(
                entity_id, actor, "push_platform_event", None, "pending",
                {"platform_event_id": platform_event_id, "asset_no": asset_no},
            )
            self.recalculate_for_asset(asset_no, reason="platform_event_pushed")
            created.append(entity)
        return created

    def claim_reconciliation(self, actor, asset_no):
        """值班员认领某台电梯的对账。两名值班员同时认领同一台电梯时，先处理的生效，
        后到的看到已被认领（确定性主键 + 数据库唯一约束）。"""
        self._require_role(actor, "duty")
        asset_no = str(asset_no or "").strip()
        if not asset_no:
            raise ValidationError("asset_no is required")
        conclusion = self._build_conclusion(asset_no)
        digest = hashlib.sha256(asset_no.encode("utf-8")).hexdigest()[:16]
        entity_id = "recon-" + digest
        existing = self.repository.get_entity(entity_id)
        if existing and existing["status"] == "active":
            raise ConflictError(
                "reconciliation for %s already claimed by %s"
                % (asset_no, existing["data"].get("claimed_by"))
            )
        data = {
            "asset_no": asset_no,
            "claimed_by": actor.user_id,
            "claimed_at": utcnow(),
            "conclusion": conclusion,
        }
        try:
            entity = self.repository.create_entity(entity_id, "reconciliation", "active", data, actor.user_id)
        except sqlite3.IntegrityError:
            current = self.repository.get_entity(entity_id)
            who = current["data"].get("claimed_by") if current else "unknown"
            raise ConflictError("reconciliation for %s already claimed by %s" % (asset_no, who))
        self.audit.record(
            entity_id, actor, "claim", None, "active",
            {"asset_no": asset_no, "summary": conclusion["summary"]},
        )
        self._sync_report_items(entity_id, conclusion)
        return entity

    def report_reconciliation(self, actor, reconciliation_id):
        """上报对账结论。上报失败后保留已认领项，只重试没写进去的。"""
        self._require_role(actor, "duty")
        recon = self.repository.get_entity(reconciliation_id)
        if not recon:
            raise NotFoundError("reconciliation not found: " + reconciliation_id)
        items = self.repository.list_report_items(reconciliation_id)
        todo = [i for i in items if i["status"] in ("pending", "failed")]
        written = 0
        failed = 0
        for item in todo:
            try:
                self.report_sink(item)
                self.repository.update_report_item_status(item["id"], "written", None, increment_attempts=False)
                written += 1
            except Exception as exc:  # noqa: BLE001 上报失败要保留待重试
                self.repository.update_report_item_status(
                    item["id"], "failed", str(exc), increment_attempts=True
                )
                failed += 1
        return {
            "reconciliation_id": reconciliation_id,
            "written": written,
            "failed": failed,
            "skipped": len(items) - len(todo),
            "items": self.repository.list_report_items(reconciliation_id),
        }

    def recover_reports(self):
        """重启后接着处理：把所有未写进平台的出报项继续重试。"""
        todo = self.repository.list_pending_report_items()
        written = 0
        failed = 0
        for item in todo:
            try:
                self.report_sink(item)
                self.repository.update_report_item_status(item["id"], "written", None, increment_attempts=False)
                written += 1
            except Exception as exc:  # noqa: BLE001
                self.repository.update_report_item_status(
                    item["id"], "failed", str(exc), increment_attempts=True
                )
                failed += 1
        return {
            "written": written,
            "failed": failed,
            "items": self.repository.list_pending_report_items(),
        }

    def list_report_items(self, actor, reconciliation_id):
        self._require_role(actor, ("admin", "duty"))
        recon = self.repository.get_entity(reconciliation_id)
        if not recon:
            raise NotFoundError("reconciliation not found: " + reconciliation_id)
        return self.repository.list_report_items(reconciliation_id)
