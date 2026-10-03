import argparse
import signal
from pathlib import Path

from src.http_api import create_server
from src.platform_sink import InMemoryPlatformSink
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def main(argv=None):
    parser = argparse.ArgumentParser(description="电梯与自动扶梯巡检和事件响应")
    parser.add_argument("--db", default="./data.db", help="SQLite database path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8336)
    args = parser.parse_args(argv)

    repository = SQLiteRepository(args.db)
    rules = RuleEngine()
    service = DomainService(repository, rules)
    # 重启后：指纹漂移的已认领结论立即重算，未写进平台的上报项保持 pending
    service.refresh_claims()
    sink = InMemoryPlatformSink()
    static_dir = Path(__file__).resolve().parent / "static"
    server = create_server(args.host, args.port, service, rules, str(static_dir), sink=sink)

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        print("电梯与自动扶梯巡检和事件响应 listening on http://%s:%s" % (args.host, args.port), flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
