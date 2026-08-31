"""Async load generator for the authorization benchmark.

Reads configuration from environment variables, issues concurrent authorized
requests against the gateway, measures end-to-end latency and the gateway's
T_auth_overhead (X-Auth-Overhead-Ns), and writes a CSV report plus P50/P99
auth-overhead statistics.

Environment variables:
    BASE_URL            Gateway base URL (e.g. http://localhost:8000)
    TOTAL_REQUESTS      Number of requests to send (default 1000)
    CONCURRENCY         Concurrent workers (default 20)
    AUTH_MODE           baseline | redis_ttl | redis_pubsub | jwt_embedded
    JWT_SECRET          Shared HS256 secret (must match the gateway)
    ACTION_DISTRIBUTION Optional comma list, e.g. "read:80,write:15,delete:5"
    USER_LIMIT          Users in the working set, 1..100 (default 100)
    RESOURCE_LIMIT      Resources in the working set, 1..50 (default 50)
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx
import jwt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("load_generator")

TOTAL_USERS = 100
TOTAL_RESOURCES = 50
ACTIONS = ("read", "write", "delete")
ADMIN_USERS = 10
EDITOR_USERS = 30  # users 11..40; the rest (41..100) are viewers


@dataclass(frozen=True)
class GeneratorConfig:
    base_url: str
    total_requests: int
    concurrency: int
    auth_mode: str
    jwt_secret: str
    action_distribution: list[str]
    user_limit: int = TOTAL_USERS
    resource_limit: int = TOTAL_RESOURCES


@dataclass
class RequestRecord:
    request_id: int
    user_id: str
    resource_id: str
    action: str
    status_code: int
    total_latency_ms: float
    auth_overhead_ns: int
    cache_hit: str = ""


@dataclass
class UserMapping:
    user_id: str
    role: str
    permissions: list[str] = field(default_factory=list)


def role_for_user(index: int) -> str:
    """Deterministic role assignment matching sql/init.sql."""
    if index <= ADMIN_USERS:
        return "admin"
    if index <= ADMIN_USERS + EDITOR_USERS:
        return "editor"
    return "viewer"


def build_user_mapping(jwt_secret: str, auth_mode: str) -> list[UserMapping]:
    """Build the fixed 100-user set with role and (for jwt_embedded) permissions."""
    users: list[UserMapping] = []
    for index in range(1, TOTAL_USERS + 1):
        role = role_for_user(index)
        user = UserMapping(user_id=f"user_{index:03d}", role=role)
        if auth_mode == "jwt_embedded":
            allowed = {"read"} if role == "viewer" else {"read", "write"}
            if role == "admin":
                allowed.add("delete")
            user.permissions = [
                f"res_{res:03d}:{action}"
                for res in range(1, TOTAL_RESOURCES + 1)
                for action in sorted(allowed)
            ]
        users.append(user)
    return users


def make_token(user: UserMapping, secret: str, auth_mode: str) -> str:
    claims: dict[str, object] = {"sub": user.user_id, "role": user.role}
    if auth_mode == "jwt_embedded":
        claims["permissions"] = user.permissions
    return jwt.encode(claims, secret, algorithm="HS256")


def load_config() -> GeneratorConfig:
    action_distribution_raw = os.getenv("ACTION_DISTRIBUTION", "").strip()
    distribution = [a.strip() for a in action_distribution_raw.split(",") if a.strip()]
    return GeneratorConfig(
        base_url=os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"),
        total_requests=int(os.getenv("TOTAL_REQUESTS", "1000")),
        concurrency=int(os.getenv("CONCURRENCY", "20")),
        auth_mode=os.getenv("AUTH_MODE", "baseline").strip().lower(),
        jwt_secret=os.getenv("JWT_SECRET", os.getenv("SECRET_KEY", "")),
        action_distribution=distribution,
        user_limit=max(1, min(TOTAL_USERS, int(os.getenv("USER_LIMIT", str(TOTAL_USERS))))),
        resource_limit=max(1, min(TOTAL_RESOURCES, int(os.getenv("RESOURCE_LIMIT", str(TOTAL_RESOURCES))))),
    )


def pick_action(rng: random.Random, distribution: list[str]) -> str:
    if not distribution:
        return rng.choice(ACTIONS)
    actions: list[str] = []
    weights: list[float] = []
    for entry in distribution:
        name, _, weight = entry.partition(":")
        actions.append(name.strip())
        weights.append(float(weight or 1))
    return rng.choices(actions, weights=weights, k=1)[0]


async def worker(
    client: httpx.AsyncClient,
    cfg: GeneratorConfig,
    users: list[UserMapping],
    rng: random.Random,
    queue: asyncio.Queue[int | None],
    results: list[RequestRecord],
) -> None:
    while True:
        request_id = await queue.get()
        if request_id is None:
            queue.task_done()
            return
        # Strided slice keeps a proportional admin/editor/viewer mix.
        stride = max(1, len(users) // cfg.user_limit)
        active_users = users[::stride][: cfg.user_limit]
        user = rng.choice(active_users)
        resource_id = f"res_{rng.randint(1, cfg.resource_limit):03d}"
        action = pick_action(rng, cfg.action_distribution)
        token = make_token(user, cfg.jwt_secret, cfg.auth_mode)

        start_ns = time.perf_counter_ns()
        status_code = 0
        auth_overhead_ns = 0
        cache_hit = ""
        try:
            response = await client.get(
                f"{cfg.base_url}/api/v1/resources/{resource_id}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Action": action,
                },
            )
            status_code = response.status_code
            overhead = response.headers.get("X-Auth-Overhead-Ns")
            auth_overhead_ns = int(overhead) if overhead else 0
            cache_hit = response.headers.get("X-Cache-Hit", "")
        except httpx.HTTPError as exc:
            logger.warning("Request %d failed: %s", request_id, exc)
        latency_ms = (time.perf_counter_ns() - start_ns) / 1_000_000

        results.append(
            RequestRecord(
                request_id=request_id,
                user_id=user.user_id,
                resource_id=resource_id,
                action=action,
                status_code=status_code,
                total_latency_ms=round(latency_ms, 3),
                auth_overhead_ns=auth_overhead_ns,
                cache_hit=cache_hit,
            )
        )
        queue.task_done()


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


def write_csv(path: str, records: list[RequestRecord]) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "request_id",
                "user_id",
                "resource_id",
                "action",
                "status_code",
                "total_latency_ms",
                "auth_overhead_ns",
                "cache_hit",
            ]
        )
        for record in records:
            writer.writerow(
                [
                    record.request_id,
                    record.user_id,
                    record.resource_id,
                    record.action,
                    record.status_code,
                    record.total_latency_ms,
                    record.auth_overhead_ns,
                    record.cache_hit,
                ]
            )
    logger.info("Wrote %d rows to %s", len(records), path)


def print_stats(records: list[RequestRecord]) -> None:
    overheads_ms = [
        r.auth_overhead_ns / 1_000_000 for r in records if r.auth_overhead_ns > 0
    ]
    latencies_ms = [r.total_latency_ms for r in records]
    logger.info(
        "Requests: %d | 2xx: %d | 403: %d | errors: %d",
        len(records),
        sum(1 for r in records if 200 <= r.status_code < 300),
        sum(1 for r in records if r.status_code == 403),
        sum(1 for r in records if r.status_code == 0),
    )
    if latencies_ms:
        logger.info(
            "E2E latency ms: P50=%.3f P99=%.3f mean=%.3f",
            percentile(latencies_ms, 50),
            percentile(latencies_ms, 99),
            statistics.fmean(latencies_ms),
        )
    if overheads_ms:
        logger.info(
            "Auth overhead ms: P50=%.6f P99=%.6f mean=%.6f",
            percentile(overheads_ms, 50),
            percentile(overheads_ms, 99),
            statistics.fmean(overheads_ms),
        )
    cache_seen = [r for r in records if r.cache_hit != ""]
    if cache_seen:
        ratio = sum(1 for r in cache_seen if r.cache_hit == "1") / len(cache_seen)
        logger.info(
            "Cache hit ratio: %.2f%% over %d cache-aware responses",
            ratio * 100,
            len(cache_seen),
        )


async def run() -> int:
    cfg = load_config()
    if not cfg.jwt_secret:
        logger.error("JWT_SECRET (or SECRET_KEY) environment variable is required")
        return 2

    users = build_user_mapping(cfg.jwt_secret, cfg.auth_mode)
    results: list[RequestRecord] = []
    queue: asyncio.Queue[int | None] = asyncio.Queue()
    rng = random.Random()

    limits = httpx.Limits(
        max_connections=cfg.concurrency * 2,
        max_keepalive_connections=cfg.concurrency,
    )
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), limits=limits) as client:
        for request_id in range(1, cfg.total_requests + 1):
            queue.put_nowait(request_id)
        workers = [
            asyncio.create_task(worker(client, cfg, users, rng, queue, results))
            for _ in range(cfg.concurrency)
        ]
        for _ in workers:
            queue.put_nowait(None)
        started = time.perf_counter()
        await queue.join()
        for task in workers:
            await task
        logger.info(
            "Completed %d requests in %.2fs (concurrency=%d, mode=%s)",
            cfg.total_requests,
            time.perf_counter() - started,
            cfg.concurrency,
            cfg.auth_mode,
        )

    csv_path = f"results_{cfg.auth_mode}_{cfg.concurrency}.csv"
    write_csv(csv_path, results)
    # print_stats(results)
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        sys.exit(130)


if __name__ == "__main__":
    main()
