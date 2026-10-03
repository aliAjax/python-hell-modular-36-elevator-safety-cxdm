"""平台困人事件与本地救援任务的对账匹配。

平台只提供"有哪些事件"（设备编号 + 报警时刻）；到场时间和完成时间
一律以本地救援任务记录为准。挂不上本地记录、或本地记录尚不完整的，
结论保留为差异（discrepancy），不做任何猜测性补全。
"""
import hashlib
import json
from datetime import datetime, timezone

DEFAULT_ALARM_WINDOW_SECONDS = 300


def parse_ts(value):
    """解析 ISO-8601 时间（兼容末尾 Z），失败返回 None。"""
    if value in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_equipment(entities, asset_no):
    for item in entities:
        if item["kind"] == "equipment" and item["data"].get("asset_no") == asset_no:
            return item
    return None


def _time_delta_seconds(ts_a, ts_b):
    a, b = parse_ts(ts_a), parse_ts(ts_b)
    if not a or not b:
        return None
    return abs(int((a - b).total_seconds()))


def compute_conclusion(event, local_entities, alarm_window_seconds=DEFAULT_ALARM_WINDOW_SECONDS):
    """根据事件与本地实体快照计算对账结论。

    返回 dict：result(matched/discrepancy)、reasons、本地关联对象及
    本地到场/完成时间、指纹（basis_fingerprint，随本地记录版本变化）。
    """
    asset_no = str(event["data"].get("asset_no", "")).strip()
    alarm_at = event["data"].get("alarm_at")

    reasons = []
    equipment = _load_equipment(local_entities, asset_no)
    alarm = None
    job = None
    delta = None

    if not equipment:
        reasons.append("equipment_not_found")
    elif parse_ts(alarm_at) is None:
        reasons.append("invalid_platform_alarm_time")
    else:
        candidates = []
        for item in local_entities:
            if item["kind"] != "alarm" or item["data"].get("equipment_id") != equipment["id"]:
                continue
            gap = _time_delta_seconds(item["data"].get("occurred_at"), alarm_at)
            if gap is not None and gap <= alarm_window_seconds:
                candidates.append((gap, item))
        if not candidates:
            reasons.append("no_local_alarm")
        else:
            delta, alarm = min(candidates, key=lambda pair: pair[0])
            jobs = [
                item
                for item in local_entities
                if item["kind"] == "rescue_job" and item["data"].get("alarm_id") == alarm["id"]
            ]
            if not jobs:
                reasons.append("no_rescue_job")
            else:
                # 同一报警可能重复派发，以最后创建的任务为准
                job = sorted(jobs, key=lambda item: (item["created_at"], item["id"]))[-1]
                if not job["data"].get("arrived_at"):
                    reasons.append("missing_arrival_time")
                if not job["data"].get("completed_at"):
                    reasons.append("missing_completion_time")

    arrived_at = job["data"].get("arrived_at") if job else None
    completed_at = job["data"].get("completed_at") if job else None

    result = "matched" if not reasons else "discrepancy"
    conclusion = {
        "result": result,
        "reasons": reasons,
        "equipment_id": equipment["id"] if equipment else None,
        "local_alarm_id": alarm["id"] if alarm else None,
        "local_rescue_job_id": job["id"] if job else None,
        # 到场/完成时间只取本地记录，平台数据不参与回填
        "local_arrived_at": arrived_at,
        "local_completed_at": completed_at,
        "alarm_time_delta_seconds": delta,
    }
    conclusion["basis_fingerprint"] = basis_fingerprint(event, [equipment, alarm, job])
    return conclusion


def basis_fingerprint(event, basis_entities):
    """指纹随事件本身或本地关联记录的状态/版本/数据变化而变化。"""
    digest = hashlib.sha256()
    digest.update(b"event\0")
    digest.update(json.dumps(
        {"id": event["id"], "status": event["status"], "data": event["data"]},
        ensure_ascii=False, sort_keys=True,
    ).encode("utf-8"))
    for entity in basis_entities:
        if entity is None:
            continue
        digest.update(b"\0entity\0")
        digest.update(json.dumps(
            {
                "id": entity["id"],
                "kind": entity["kind"],
                "status": entity["status"],
                "version": entity["version"],
                "data": entity["data"],
            },
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8"))
    return digest.hexdigest()
