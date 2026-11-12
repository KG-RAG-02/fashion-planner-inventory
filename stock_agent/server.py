"""Start/check Stock using the packaged service manifest."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.request import ProxyHandler, build_opener


def manifest():
    return json.loads(Path(__file__).with_name("service-manifest.json").read_text())


def main():
    config = manifest()
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=config["host_bind"])
    parser.add_argument("--check-ready", action="store_true")
    args = parser.parse_args()
    if args.check_ready:
        try:
            with build_opener(ProxyHandler({})).open(
                    f'http://127.0.0.1:{config["port"]}{config["readiness_path"]}', timeout=4) as response:
                return 0 if response.status == 200 else 1
        except Exception:
            return 1
    import uvicorn
    uvicorn.run("stock_agent.api:app", host=args.host, port=config["port"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
