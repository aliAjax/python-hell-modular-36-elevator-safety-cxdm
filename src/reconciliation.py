"""城市应急平台困人报警对账。

平台每天推来困人报警清单，只交代“有哪些事件”（平台事件编号、设备编号、报警时刻）。
对账时按设备编号(asset_no)和报警时刻把平台事件挂到本地救援任务；到场与完成时间一律以
本地救援记录为准，平台给出的时间仅作比对。对不上的部分保留为差异项。

匹配规则：
- 设备编号相同是前提；
- 报警时刻归一化到 UTC，相差在 ALARM_MATCH_TOLERANCE_SECONDS 内视为同一事件，挂到时刻最接近的本地救援任务；
- 挂上后，本地缺失到场/完成时间，或平台给的时间与本地相差超过 TIME_MISMATCH_TOLERANCE_SECONDS，记为差异；
- 本地有救援任务但平台清单里没有，记为“本地救援未上报”差异。
"""

from datetime import datetime, timezone

ALARM_MATCH_TOLERANCE_SECONDS = 300  # 报警时刻匹配容差：5 分钟
TIME_MISMATCH_TOLERANCE_SECONDS = 60  # 到场/完成时间比对容差：1 分钟

REASON_NO_LOCAL_RESCUE = "no_local_rescue"
REASON_NO_MATCHING_ALARM_TIME = "no_matching_alarm_time"
REASON_MISSING_ARRIVAL = "missing_arrival"
REASON_MISSING_COMPLETION = "missing_completion"
REASON_ARRIVAL_MISMATCH = "arrival_time_mismatch"
REASON_COMPLETION_MISMATCH = "completion_time_mismatch"
REASON_LOCAL_RESCUE_NOT_REPORTED = "local_rescue_without_platform_event"


def _parse_ts(value):
    """把 ISO-8601 字符串解析为 UTC datetime；无法解析返回 None。"""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _delta_seconds(a, b):
    if a is None or b is None:
        return None
    return abs((a - b).total_seconds())


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _platform_item(event, rescue_job_id, status, reasons, local_arrived, local_completed):
    data = event["data"]
    return {
        "item_key": str(data.get("platform_event_id")),
        "platform_event_id": data.get("platform_event_id"),
        "asset_no": data.get("asset_no"),
        "alarm_at": data.get("alarm_at"),
        "rescue_job_id": rescue_job_id,
        "arrived_at": local_arrived,
        "completed_at": local_completed,
        "platform_arrived_at": data.get("arrived_at"),
        "platform_completed_at": data.get("completed_at"),
        "status": status,
        "reasons": list(reasons),
    }


def _local_item(record):
    return {
        "item_key": "local:" + str(record["rescue_job_id"]),
        "platform_event_id": None,
        "asset_no": record["asset_no"],
        "alarm_at": record["alarm_at"],
        "rescue_job_id": record["rescue_job_id"],
        "arrived_at": record.get("arrived_at"),
        "completed_at": record.get("completed_at"),
        "platform_arrived_at": None,
        "platform_completed_at": None,
        "status": "discrepancy",
        "reasons": [REASON_LOCAL_RESCUE_NOT_REPORTED],
    }


def build_conclusion(asset_no, platform_events, local_records):
    """根据平台事件与本地救援记录生成对账结论。

    platform_events: platform_event 实体列表（data 含 platform_event_id/asset_no/alarm_at/...）
    local_records: 本地记录字典列表，含 rescue_job_id/alarm_at/asset_no/arrived_at/completed_at
    返回 {"asset_no", "computed_at", "items", "summary"}。
    """
    items = []
    attached = set()

    for event in platform_events:
        data = event["data"]
        platform_alarm = _parse_ts(data.get("alarm_at"))
        candidates = [r for r in local_records if r.get("asset_no") == asset_no]

        if not candidates:
            items.append(_platform_item(event, None, "discrepancy", [REASON_NO_LOCAL_RESCUE], None, None))
            continue

        best = None
        best_delta = None
        for record in candidates:
            local_alarm = _parse_ts(record.get("alarm_at"))
            delta = _delta_seconds(platform_alarm, local_alarm)
            if delta is None:
                continue
            if best_delta is None or delta < best_delta:
                best = record
                best_delta = delta

        if best is None or best_delta > ALARM_MATCH_TOLERANCE_SECONDS:
            items.append(
                _platform_item(
                    event,
                    None,
                    "discrepancy",
                    [REASON_NO_MATCHING_ALARM_TIME],
                    None,
                    None,
                )
            )
            continue

        attached.add(best["rescue_job_id"])
        reasons = []
        local_arrived = best.get("arrived_at")
        local_completed = best.get("completed_at")

        if not local_arrived:
            reasons.append(REASON_MISSING_ARRIVAL)
        else:
            platform_arrived = _parse_ts(data.get("arrived_at"))
            if platform_arrived is not None:
                delta = _delta_seconds(platform_arrived, _parse_ts(local_arrived))
                if delta is not None and delta > TIME_MISMATCH_TOLERANCE_SECONDS:
                    reasons.append(REASON_ARRIVAL_MISMATCH)

        if not local_completed:
            reasons.append(REASON_MISSING_COMPLETION)
        else:
            platform_completed = _parse_ts(data.get("completed_at"))
            if platform_completed is not None:
                delta = _delta_seconds(platform_completed, _parse_ts(local_completed))
                if delta is not None and delta > TIME_MISMATCH_TOLERANCE_SECONDS:
                    reasons.append(REASON_COMPLETION_MISMATCH)

        status = "matched" if not reasons else "discrepancy"
        items.append(_platform_item(event, best["rescue_job_id"], status, reasons, local_arrived, local_completed))

    for record in local_records:
        if record["rescue_job_id"] not in attached:
            items.append(_local_item(record))

    summary = {
        "total": len(items),
        "matched": sum(1 for i in items if i["status"] == "matched"),
        "discrepancy": sum(1 for i in items if i["status"] == "discrepancy"),
    }
    return {
        "asset_no": asset_no,
        "computed_at": _now(),
        "items": items,
        "summary": summary,
    }


class ReportSink:
    """上报模拟器。默认全部成功；可通过 fail_keys 让指定 item_key 上报失败，用于演示重试。"""

    def __init__(self, fail_keys=None):
        self.fail_keys = set(fail_keys or ())

    def __call__(self, item):
        if item.get("item_key") in self.fail_keys:
            raise RuntimeError("simulated report failure for " + str(item.get("item_key")))
