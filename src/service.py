import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .reconcile import DEFAULT_ALARM_WINDOW_SECONDS, compute_conclusion, parse_ts
from .rules import RuleEngine

DUTY_ROLES = ("dispatcher", "admin")
TRACKED_KINDS = ("equipment", "alarm", "rescue_job")


class DomainService:
    def __init__(self, repository, rules=None, alarm_window_seconds=DEFAULT_ALARM_WINDOW_SECONDS):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        self.alarm_window_seconds = alarm_window_seconds

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _require_duty(self, actor):
        if actor.role not in DUTY_ROLES:
            raise PermissionDenied("only duty dispatchers may submit reconciliation claims")

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
        # 本地报警、救援或设备记录一旦出现，已认领结论同样立即作废重算
        if entity["kind"] in TRACKED_KINDS:
            self._invalidate_for(entity, actor)
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
        # 本地报警、救援或设备状态一变，已认领结论立即作废重算
        if updated["kind"] in TRACKED_KINDS:
            self._invalidate_for(updated, actor)
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

    # ------------------------------------------------------------------
    # 平台事件导入 / 对账认领 / 上报
    # ------------------------------------------------------------------

    def import_platform_events(self, actor, events):
        """接收城市应急平台推来的困人事件清单。

        平台只交代有哪些事件：设备编号(asset_no)和报警时刻(alarm_at)。
        幂等键 (source, event_ref) 保证重复推送不产生重复事件。
        """
        self._require_duty(actor)
        if not isinstance(events, list):
            raise ValidationError("events must be a list")
        imported = []
        for raw in events:
            if not isinstance(raw, dict):
                raise ValidationError("each platform event must be an object")
            source = str(raw.get("source", "city_platform")).strip()
            event_ref = str(raw.get("event_ref", "")).strip()
            asset_no = str(raw.get("asset_no", "")).strip()
            alarm_at = raw.get("alarm_at")
            if not event_ref:
                raise ValidationError("event_ref is required")
            if not asset_no:
                raise ValidationError("asset_no is required")
            if parse_ts(alarm_at) is None:
                raise ValidationError("alarm_at must be ISO-8601")
            event_id = "pe-" + hashlib.sha256(
                (source + "\0" + event_ref).encode("utf-8")
            ).hexdigest()[:24]
            existing = self.repository.get_entity(event_id)
            if existing:
                imported.append(existing)
                continue
            payload = {
                "source": source,
                "event_ref": event_ref,
                "asset_no": asset_no,
                "alarm_at": str(alarm_at),
                "address": raw.get("address", ""),
                "summary": raw.get("summary", ""),
            }
            entity = self.repository.create_entity(
                event_id, "platform_event", "reported", payload, actor.user_id
            )
            self.audit.record(event_id, actor, "platform_import", None, "reported", {"event_ref": event_ref})
            imported.append(entity)
        return imported

    def _snapshot(self):
        return self.repository.list_entities()

    def claim_event(self, actor, event_id, note=None):
        """值班员把平台事件对账认领挂到本地救援任务。

        同一台电梯事件并发认领时，先提交的生效；后到的看到 ConflictError。
        """
        self._require_duty(actor)
        event = self.repository.get_entity(event_id)
        if not event or event["kind"] != "platform_event":
            raise NotFoundError("platform event not found: " + str(event_id))

        def build():
            conclusion = compute_conclusion(event, self._snapshot(), self.alarm_window_seconds)
            data = {
                "platform_event_id": event_id,
                "asset_no": event["data"]["asset_no"],
                "platform_alarm_at": event["data"]["alarm_at"],
                "claimed_by": actor.user_id,
                "note": note or "",
                "recompute_count": 0,
                **conclusion,
            }
            return data["result"], data

        claim = self.repository.insert_claim(
            "rc-" + uuid4().hex[:24], event_id, actor.user_id, build
        )
        updated_event = self.repository.update_entity(
            event_id, event["version"], "reconciled", event["data"]
        )
        self.audit.record(
            claim["id"], actor, "claim", None, claim["status"],
            {"event_id": event_id, "result": claim["data"]["result"]},
        )
        self.audit.record(event_id, actor, "reconcile", "reported", "reconciled", {"claim_id": claim["id"]})
        self.enqueue_report(claim)
        return claim

    def enqueue_report(self, claim):
        payload = {
            "claim_id": claim["id"],
            "platform_event_id": claim["data"]["platform_event_id"],
            "asset_no": claim["data"]["asset_no"],
            "result": claim["data"]["result"],
            "reasons": claim["data"]["reasons"],
            "local_alarm_id": claim["data"]["local_alarm_id"],
            "local_rescue_job_id": claim["data"]["local_rescue_job_id"],
            # 到场、完成时间以本地记录为准
            "local_arrived_at": claim["data"]["local_arrived_at"],
            "local_completed_at": claim["data"]["local_completed_at"],
        }
        # 重算后内容以最新结论为准；未确认成功的项下次重试最新值
        self.repository.upsert_outbox(claim["id"], claim["data"]["platform_event_id"], payload)

    def flush_reports(self, sink):
        """只重试 outbox 中还没写进平台的 pending 项；sent 项保留不动。"""
        results = []
        for item in self.repository.list_outbox(status="pending"):
            try:
                sink.send(item["payload"])
            except Exception as exc:  # 平台写失败：保留待重试
                self.repository.mark_outbox_failed(item["claim_id"], exc)
                results.append({"claim_id": item["claim_id"], "status": "pending", "error": str(exc)})
            else:
                self.repository.mark_outbox_sent(item["claim_id"])
                results.append({"claim_id": item["claim_id"], "status": "sent"})
        return results

    def list_outbox(self, status=None):
        return self.repository.list_outbox(status=status)

    def _claims_for_changed(self, changed):
        """找出与变更实体相关的已认领结论。"""
        asset_no = None
        if changed["kind"] == "equipment":
            asset_no = changed["data"].get("asset_no")
        elif changed["kind"] == "alarm":
            equipment = self.repository.get_entity(changed["data"].get("equipment_id", ""))
            asset_no = equipment["data"].get("asset_no") if equipment else None
        elif changed["kind"] == "rescue_job":
            alarm = self.repository.get_entity(changed["data"].get("alarm_id", ""))
            if alarm:
                equipment = self.repository.get_entity(alarm["data"].get("equipment_id", ""))
                asset_no = equipment["data"].get("asset_no") if equipment else None
        claims = []
        for claim in self.repository.list_entities(kind="reconciliation"):
            data = claim["data"]
            related = changed["id"] in (
                data.get("equipment_id"),
                data.get("local_alarm_id"),
                data.get("local_rescue_job_id"),
            )
            # 旧结论尚未挂上本地记录时，按同设备编号兜底关联
            if not related and asset_no and data.get("asset_no") == asset_no:
                related = True
            if related:
                claims.append(claim)
        return claims

    def _invalidate_for(self, changed, actor=None):
        for claim in self._claims_for_changed(changed):
            self._recompute(claim, reason="source_changed:" + changed["kind"])

    def refresh_claims(self, actor=None):
        """启动/巡检兜底：指纹漂移（含库外改动）的已认领结论全部重算。"""
        refreshed = []
        for claim in self.repository.list_entities(kind="reconciliation"):
            event = self.repository.get_entity(claim["data"]["platform_event_id"])
            if not event:
                continue
            conclusion = compute_conclusion(event, self._snapshot(), self.alarm_window_seconds)
            if conclusion["basis_fingerprint"] != claim["data"].get("basis_fingerprint"):
                refreshed.append(self._recompute(claim, reason="basis_drift"))
        return refreshed

    def _recompute(self, claim, reason):
        """作废并重算：认领关系保留（还是原值班员认领的），结论立即更新。"""
        event = self.repository.get_entity(claim["data"]["platform_event_id"])
        if not event:
            return claim
        conclusion = compute_conclusion(event, self._snapshot(), self.alarm_window_seconds)
        data = dict(claim["data"])
        data.update(conclusion)
        data["recompute_count"] = int(data.get("recompute_count", 0)) + 1
        updated = self.repository.update_entity(claim["id"], claim["version"], conclusion["result"], data)
        from .domain import Actor
        sys_actor = Actor("system", "admin")
        self.audit.record(
            claim["id"], sys_actor, "recompute", claim["status"], updated["status"],
            {"reason": reason, "result": conclusion["result"], "reasons": conclusion["reasons"]},
        )
        self.enqueue_report(updated)
        return updated
