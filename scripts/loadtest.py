"""Local load test: N concurrent MCP clients calling write/upsert_person/patch/read.

Runs entirely in-process against a generated, fictional vault, on LocalStorage or on S3 mocked
with moto (optionally with a simulated per-request round-trip latency). Never point this at
production.

    uv run python scripts/loadtest.py --backend s3 --notes 400 --latency-ms 20
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import statistics
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

from fastmcp import Client

from second_brain.config import Settings
from second_brain.server import build_server
from second_brain.storage import LocalStorage, S3Storage
from second_brain.vault import Vault

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "vault"


class CountingStorage:
    """Wraps a Storage: counts calls per method and sleeps `latency` seconds on each one."""

    def __init__(self, inner, latency: float):
        self.inner, self.latency = inner, latency
        self.calls: Counter[str] = Counter()
        self._lock = threading.Lock()

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            with self._lock:
                self.calls[name] += 1
            if self.latency:
                time.sleep(self.latency)
            return attr(*args, **kwargs)

        return call


def seed(storage, notes: int) -> None:
    for p in sorted(EXAMPLE.rglob("*.md")):
        storage.write(p.relative_to(EXAMPLE).as_posix(), p.read_text(encoding="utf-8"))
    for i in range(notes):
        body = f"# Topic {i:04d}\n\nFictional filler about widgets and [[Topic {(i + 1) % notes:04d}]].\n" * 3
        storage.write(f"Resources/Load/Topic {i:04d}.md", f"---\ntype: resource\ntags: [load]\n---\n{body}")


async def client_session(server, cid: int, rounds: int, timings: list[tuple[str, float]]) -> None:
    async with Client(server) as c:
        for r in range(rounds):
            calls = [
                ("write", {"folder": "Resources/Load", "name": f"Client {cid} note {r}", "body": "hello"}),
                ("upsert_person", {"name": f"Load Person {cid}", "notes": f"fact {r}"}),
                ("patch", {"name": f"Topic {(cid * 7 + r) % 50:04d}", "append": f"- client {cid} round {r}"}),
                ("read", {"names": [f"Topic {r:04d}", "Dana Rivers"]}),
            ]
            for tool, args in calls:
                t0 = time.perf_counter()
                await c.call_tool(tool, args)
                timings.append((tool, time.perf_counter() - t0))


async def run(args) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="sb-load-"))
    mock = None
    try:
        if args.backend == "s3":
            import boto3
            from moto import mock_aws

            mock = mock_aws()
            mock.start()
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="load")
            inner = S3Storage("load", prefix="vault", client=s3)
        else:
            inner = LocalStorage(tmp)
        seed(inner, args.notes)
        storage = CountingStorage(inner, args.latency_ms / 1000)
        server = build_server(Settings(transport="stdio", auth="none", stats=False), vault=Vault(storage))

        timings: list[tuple[str, float]] = []
        t0 = time.perf_counter()
        await asyncio.gather(*(client_session(server, i, args.rounds, timings) for i in range(args.clients)))
        wall = time.perf_counter() - t0

        lat = sorted(t for _, t in timings)
        p = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))]  # noqa: E731
        print(
            f"backend={args.backend} notes={args.notes} latency={args.latency_ms}ms "
            f"clients={args.clients} rounds={args.rounds}"
        )
        print(f"  calls={len(lat)} wall={wall:.2f}s throughput={len(lat) / wall:.1f} calls/s")
        print(
            f"  latency p50={p(0.5) * 1000:.0f}ms p95={p(0.95) * 1000:.0f}ms max={lat[-1] * 1000:.0f}ms "
            f"mean={statistics.mean(lat) * 1000:.0f}ms"
        )
        print(f"  storage calls: {dict(sorted(storage.calls.items()))}")
    finally:
        if mock:
            mock.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=["local", "s3"], default="s3")
    ap.add_argument("--notes", type=int, default=400, help="generated notes on top of the example vault")
    ap.add_argument("--latency-ms", type=float, default=0, help="simulated round trip per storage call")
    ap.add_argument("--clients", type=int, default=5)
    ap.add_argument("--rounds", type=int, default=3, help="write+upsert_person+patch+read rounds per client")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
