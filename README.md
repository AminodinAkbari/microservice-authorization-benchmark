# authz-benchmark

Microservices authorization benchmark for a master's thesis on **authorization
latency optimization**. A FastAPI gateway fronts a protected resource endpoint
and enforces authorization using one of four strategies (AUTH_MODE), each with
a very different latency profile. An async load generator measures end-to-end
latency and the gateway's own authorization overhead (`T_auth_overhead`).

## Tech stack

- Python 3.11, FastAPI, httpx (async), redis.asyncio (Redis 6+/7), asyncpg
  (PostgreSQL 14+), PyJWT (HS256)
- Docker + docker-compose
- Kubernetes via Kind (1 control-plane + 2 workers)

## Architecture

```
                       +---------------------------+
   load generator ---> |  gateway  (FastAPI :8000) |
   (httpx, async)      |  JWT verify + AUTH_MODE   |
                       +-----+----------+----------+
                             |          |
                 baseline/   |          |  redis_ttl / redis_pubsub
                 cache miss  |          |
                             v          v
                  +----------------+   +-------+
                  | auth_service   |   | Redis |
                  | (FastAPI:8001) |   | :6379 |
                  | asyncpg        |   +-------+
                  +-------+--------+     ^ publish authz:invalidate
                          |              |
                          v              |
                  +----------------+     |
                  | PostgreSQL 14  |-----+
                  +----------------+
```

- **gateway** (`gateway/`): verifies the JWT (PyJWT, HS256, `SECRET_KEY` env),
  extracts `sub` as `user_id`, then enforces the decision per `AUTH_MODE`.
  Measures `T_auth_overhead` with `time.perf_counter_ns()` in middleware and
  returns it as the `X-Auth-Overhead-Ns` response header (plus `X-Cache-Hit`
  for cache-aware modes).
- **auth_service** (`auth_service/`): `POST /check` resolves
  `user -> role_id -> role_permissions` in PostgreSQL (asyncpg) and returns
  `{"allow": 1|0}`. `POST /admin/users/{user_id}/role` updates the role,
  commits, and publishes `{"user_id": ...}` to the Redis channel
  `authz:invalidate`.
- **load_generator** (`load_generator/`): async httpx client, N requests with
  configurable concurrency, writes `results_{AUTH_MODE}_{CONCURRENCY}.csv` and
  prints P50/P99 auth overhead.
- **sql/init.sql**: schema + seed data (3 roles, permissions, 100 users).
- **k8s/**: Kind manifests; **docker-compose.yaml**: local stack.

### The four AUTH_MODE scenarios

| Mode | Decision path | Expected auth overhead |
|---|---|---|
| `baseline` | JWT verify + HTTP POST to auth_service `/check` (asyncpg queries) | ~1-2 ms |
| `redis_ttl` | Redis GET `authz:{user_id}:{resource_id}:{action}`; on miss call auth_service then `SET ... EX 60` | ~250-400 µs (hit) |
| `redis_pubsub` | Same as `redis_ttl`, plus a background subscriber on `authz:invalidate` that SCANs and deletes `authz:{user_id}:*` when a role changes | ~250-400 µs (hit), near-zero staleness |
| `jwt_embedded` | Decision from the `permissions` claim in the JWT (`["res_001:read", ...]`); no Redis, no auth_service | ~30-50 µs |

Cache values are stored as bytes `b"1"`/`b"0"` and decoded on GET. After
warm-up (first access per user/resource/action triple), the cache hit ratio is
expected to exceed **95%** because the workload uses 100 users x 50 resources
x up to 3 actions with TTL 60 s.

## Project layout

```
authz-benchmark/
├── gateway/            # FastAPI gateway (middleware, redis client, JWT utils)
├── auth_service/       # FastAPI authz service (asyncpg + Redis publish)
├── load_generator/     # async benchmark driver
├── sql/init.sql        # schema + seed data
├── k8s/                # Kind manifests
├── docker-compose.yaml # local stack
├── kind-config.yaml    # Kind cluster: 1 control-plane + 2 workers
└── README.md
```

## Running locally with docker-compose

1. Create a `.env` next to `docker-compose.yaml`:

   ```env
   SECRET_KEY=super-secret-benchmark-key
   AUTH_MODE=baseline
   POSTGRES_USER=authz
   POSTGRES_PASSWORD=authz
   POSTGRES_DB=authzdb
   ```

2. Build and start the stack:

   ```bash
   docker compose up -d --build
   docker compose ps            # wait for postgres/redis healthy
   curl http://localhost:8000/health
   curl http://localhost:8001/health
   ```

3. Run the load generator from the project root (local Python 3.11):

   ```bash
   python3.11 -m venv .venv && source .venv/bin/activate
   pip install -r load_generator/requirements.txt

   export BASE_URL=http://localhost:8000
   export TOTAL_REQUESTS=5000
   export CONCURRENCY=50
   export AUTH_MODE=baseline
   export JWT_SECRET=super-secret-benchmark-key

   python -m load_generator.generator
   ```

   Results land in `results_baseline_50.csv` (columns: `request_id`,
   `user_id`, `resource_id`, `action`, `status_code`, `total_latency_ms`,
   `auth_overhead_ns`, `cache_hit`) and P50/P99 auth overhead (ms) is printed.

   Optional action mix (defaults to uniform over read/write/delete):

   ```bash
   export ACTION_DISTRIBUTION=read:80,write:15,delete:5
   ```

### Changing AUTH_MODE

`AUTH_MODE` is read by the gateway at startup. To benchmark another strategy:

```bash
export AUTH_MODE=redis_ttl        # or baseline / redis_pubsub / jwt_embedded
docker compose up -d --build gateway   # recreate the gateway only
# then rerun the load generator with AUTH_MODE matching
```

With compose you can also set `AUTH_MODE` in `.env` and run
`docker compose up -d --force-recreate gateway`.

### Testing role changes (redis_pubsub invalidation)

```bash
# 1. Warm the cache for user_001, then change their role:
curl -X POST http://localhost:8001/admin/users/user_001/role \
     -H 'Content-Type: application/json' -d '{"role": "viewer"}'
# The gateway subscribed to authz:invalidate deletes all authz:user_001:* keys,
# so the next /check for that user goes back to auth_service.
```

## Running on Kubernetes with Kind

1. Create the cluster (1 control-plane + 2 workers):

   ```bash
   kind create cluster --name authz-benchmark --config kind-config.yaml
   kubectl cluster-info --context kind-authz-benchmark
   ```

2. Build and load the images into Kind:

   ```bash
   docker build -t authz-benchmark/gateway:latest ./gateway
   docker build -t authz-benchmark/auth-service:latest ./auth_service
   kind load docker-image authz-benchmark/gateway:latest --name authz-benchmark
   kind load docker-image authz-benchmark/auth-service:latest --name authz-benchmark
   ```

3. Apply the manifests:

   ```bash
   kubectl apply -f k8s/redis-deployment.yaml
   kubectl apply -f k8s/auth-service-deployment.yaml
   kubectl apply -f k8s/gateway-deployment.yaml
   kubectl get pods,svc
   ```

   All Services are `ClusterIP`. Redis and auth_service/gateway config come
   from the `authz-config` ConfigMap and `authz-secret` Secret defined in
   `k8s/gateway-deployment.yaml`. Update `AUTH_MODE` in the ConfigMap and
   restart the gateway deployment to switch scenarios:

   ```bash
   kubectl -n default set env deployment/gateway AUTH_MODE=redis_ttl
   kubectl -n default rollout restart deployment/gateway
   ```

4. PostgreSQL: the manifests assume PostgreSQL runs **outside** the cluster;
   `k8s/auth-service-deployment.yaml` contains a placeholder `ExternalName`
   Service named `postgres` (edit `externalName:` to your PostgreSQL 14+ host,
   e.g. `postgres.default.svc.cluster.local` or a LAN hostname). Update the
   `DATABASE_URL` in `authz-config` accordingly and load the seed data:

   ```bash
   psql "postgresql://authz:authz@<pg-host>:5432/authzdb" -f sql/init.sql
   ```

   To deploy PostgreSQL **inside** Kind instead, delete the ExternalName
   Service and apply a standard postgres:14-alpine Deployment + ClusterIP
   Service on port 5432 (mount `sql/init.sql` at
   `/docker-entrypoint-initdb.d/init.sql` via a ConfigMap).

5. Access the gateway (ClusterIP) for benchmarking, e.g. port-forward:

   ```bash
   kubectl port-forward svc/gateway 8000:8000
   BASE_URL=http://localhost:8000 python -m load_generator.generator
   ```

## Load generator usage and concurrency sweeps

```bash
export BASE_URL=http://localhost:8000
export JWT_SECRET=super-secret-benchmark-key
export TOTAL_REQUESTS=10000

for MODE in baseline redis_ttl redis_pubsub jwt_embedded; do
  for C in 10 25 50 100; do
    export AUTH_MODE=$MODE
    export CONCURRENCY=$C
    # restart gateway with the new AUTH_MODE before each run (compose/k8s)
    python -m load_generator.generator
  done
done
```

Fixed workload identity: users `user_001`..`user_100`
(1-10 admin, 11-40 editor, 41-100 viewer), resources `res_001`..`res_050`,
JWTs signed HS256 with `JWT_SECRET` (must equal the gateway's `SECRET_KEY`).
For `jwt_embedded` the generator embeds the full `permissions` claim per role.

## Expected results (thesis reference points)

- `baseline`: auth overhead ~**1-2 ms** (JWT verify + HTTP hop + 2 SQL queries)
- `redis_ttl`: ~**250-400 µs** on cache hits
- `jwt_embedded`: ~**30-50 µs** (no network/DB after JWT verification)
- Cache hit ratio **> 95%** after warm-up for `redis_ttl`/`redis_pubsub`
- `X-Auth-Overhead-Ns` (nanoseconds) is the thesis metric `T_auth_overhead`;
  the CSV also records end-to-end latency and cache hits.

## Environment variables reference

| Variable | Used by | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | gateway, load generator | required | Shared HS256 JWT secret |
| `AUTH_MODE` | gateway | `baseline` | `baseline`, `redis_ttl`, `redis_pubsub`, `jwt_embedded` |
| `AUTH_SERVICE_URL` | gateway | `http://localhost:8001` | auth_service base URL |
| `REDIS_URL` | gateway, auth_service | `redis://localhost:6379/0` | Redis DSN |
| `CACHE_TTL_SECONDS` | gateway | `60` | TTL for cached decisions |
| `INVALIDATE_CHANNEL` | gateway, auth_service | `authz:invalidate` | Pub/sub channel |
| `DATABASE_URL` | auth_service | required | PostgreSQL DSN (asyncpg) |
| `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE` | auth_service | `5` / `20` | asyncpg pool bounds |
| `BASE_URL` | load generator | `http://localhost:8000` | Gateway base URL |
| `TOTAL_REQUESTS` | load generator | `1000` | Number of requests |
| `CONCURRENCY` | load generator | `20` | Concurrent workers |
| `JWT_SECRET` | load generator | `SECRET_KEY` | Signing secret for generated JWTs |
| `ACTION_DISTRIBUTION` | load generator | uniform | e.g. `read:80,write:15,delete:5` |
| `USER_LIMIT` | load generator | `100` | Working-set size: users 1..100 (strided, keeps role mix) |
| `RESOURCE_LIMIT` | load generator | `50` | Working-set size: resources 1..50 |

## Reproducing the >95% cache hit ratio

The full identity space is 100 users x 50 resources x up to 3 actions = up to
15,000 distinct authorization keys. A run of N requests can only achieve a hit
ratio near `1 - min(1, keys/N)`, so to demonstrate the post-warm-up ratio,
shrink the working set and warm the cache first:

```bash
# Warm-up sweep (populates authz:* keys), then a measured run:
USER_LIMIT=10 RESOURCE_LIMIT=10 TOTAL_REQUESTS=1000 CONCURRENCY=50 AUTH_MODE=redis_ttl \
  python -m load_generator.generator   # first pass fills the cache
USER_LIMIT=10 RESOURCE_LIMIT=10 TOTAL_REQUESTS=5000 CONCURRENCY=50 AUTH_MODE=redis_ttl \
  python -m load_generator.generator   # measured pass: expect >95% cache hits
```
