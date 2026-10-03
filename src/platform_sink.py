"""对账结论向城市应急平台上报的出站通道。

上报失败不影响本地认领：outbox 中已写入的认领项保留，只有没写进去
（未确认成功）的项继续重试，进程重启后接着处理 pending 项。
"""


class ReportError(Exception):
    """平台接收失败，该项保持 pending 等待重试。"""


class InMemoryPlatformSink:
    """测试与演示用的内存通道；received 项视为平台已确认写入。"""

    def __init__(self, fail_claim_ids=None):
        self.received = []
        self.fail_claim_ids = set(fail_claim_ids or [])

    def send(self, item):
        if item["claim_id"] in self.fail_claim_ids:
            raise ReportError("platform rejected: " + item["claim_id"])
        self.received.append(item)
        return True
