# Mini Escrow Service (Django + DRF)

This project implements the **Mini Escrow Service** backend described in the assignment using:

- Django + Django REST Framework
- MySQL
- Celery (with a scheduled expiration task)
- Swagger / OpenAPI via `drf-yasg`

The focus is on **state machine design**, **race condition handling**, **permissions**, and **tests**.

---

## Domain & State Machine

Each `Escrow` represents a transaction between:

- a **Buyer** (`buyer_id`)
- a **Seller** (`seller_id`)

Key fields (see `escrow/models.py`):

- `buyer_id`, `seller_id`
- `amount`, `currency`
- `state` – one of:
  - `CREATED`
  - `FUNDED`
  - `RELEASED`
  - `REFUNDED`
  - `EXPIRED`
- `expires_at` – set when the escrow is funded
- Timestamps for each terminal transition (`funded_at`, `released_at`, `refunded_at`, `expired_at`)
- `version` – simple optimistic locking counter

### Allowed State Transitions

- `CREATED → FUNDED` (`Escrow.fund`)
- `FUNDED → RELEASED` (`Escrow.release`)
- `FUNDED → REFUNDED` (`Escrow.refund`)
- `FUNDED → EXPIRED` (`Escrow.expire`)

Invalid transitions are explicitly prevented:

- Each transition method checks the **current state** and raises `ValueError` on invalid transitions (covered by tests in `EscrowModelStateMachineTests`).
- All transition methods are `@transaction.atomic` to ensure consistency.

### Callbacks

Every state change goes through `_set_state`, which:

- Records the previous and new state
- Bumps the `version` field
- Logs a structured **state change event** via `logging` (this can be swapped for webhooks / notifications).

---

## Authentication & Authorization

### Simple Header-Based Identification

Implemented in `escrow/auth.py` as `HeaderUserAuthentication`:

- `X-User-Id` → `request.user.id`
- `X-User-Role` → `request.user.role` (`buyer` or `seller`)

If either header is missing or invalid, authentication fails.

Configured in `REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES']` to be the only auth mechanism.

### Permission Rules

Implemented in `escrow/permissions.py`:

- **`IsEscrowParticipant`**
  - Buyer:
    - Can see and act only on escrows where `buyer_id == user.id`.
  - Seller:
    - Can only view escrows where `seller_id == user.id` (safe methods only).
    - Cannot perform write operations (fund/release/refund).
- **`BuyerOnly`**
  - Ensures only users with `role == 'buyer'` can access certain endpoints.

These rules are enforced at the **API layer**, covering all escrow endpoints.

---

## API Design

Main implementation in `escrow/views.py` (`EscrowViewSet`) and `escrow/serializers.py`.

Base URL prefix: `/api/`

### Endpoints

- **Create escrow**
  - `POST /api/escrows/`
  - Buyer is taken from `X-User-Id` header.
  - `seller_id`, `amount`, `currency` come from the request body.

- **List escrows**
  - `GET /api/escrows/`
  - Buyer: lists own escrows.
  - Seller: lists escrows where they are the seller.

- **Retrieve escrow**
  - `GET /api/escrows/{id}/`
  - Restricted by `IsEscrowParticipant`.

- **Fund escrow**
  - `POST /api/escrows/{id}/fund/`
  - Allowed only for buyer (`BuyerOnly`).
  - `CREATED → FUNDED`, sets `expires_at` based on `ESCROW_DEFAULT_EXPIRATION_SECONDS`.

- **Release funds**
  - `POST /api/escrows/{id}/release/`
  - Allowed only for buyer.
  - `FUNDED → RELEASED`.

- **Refund escrow**
  - `POST /api/escrows/{id}/refund/`
  - Allowed only for buyer.
  - `FUNDED → REFUNDED`.

### API Documentation

Swagger / OpenAPI is provided via `drf-yasg` in `escrow_service/urls.py`:

- Swagger UI: `GET /swagger/`
- JSON schema: `GET /swagger.json`

---

## Expiration Logic & Celery

### Expiration Semantics

- When an escrow transitions to `FUNDED`, its `expires_at` is set to:
  - `now + ESCROW_DEFAULT_EXPIRATION_SECONDS`
  - Default: 24 hours (configurable in `escrow_service/settings.py`).
- If no action is taken before `expires_at`, the escrow should automatically become `EXPIRED`.

### Celery Task

Implementation: `escrow/tasks.py` – `expire_funded_escrows`.

Behavior:

- Finds escrows that are:
  - `state = FUNDED`
  - `expires_at <= now`
- Processes them in small batches.
- For each escrow:
  - Opens a transaction and re-loads the row using `select_for_update` to lock it.
  - Re-checks that the state is still `FUNDED` (another actor may have changed it).
  - Calls `Escrow.expire()` to move it to `EXPIRED`.
- Returns the number of escrows expired.
- **Idempotent**: running it multiple times does not double-expire any escrow.

### Scheduling

The project includes `django-celery-beat` so you can configure a periodic task (e.g. every minute) via:

1. Running the Django admin (`/admin/`).
2. Adding a periodic task pointing to `escrow.tasks.expire_funded_escrows`.

You can also schedule using plain Celery beat configuration if preferred.

---

## Race Conditions & Concurrency Handling

Key race scenarios considered:

1. **Two simultaneous requests: RELEASE and REFUND**
2. **Expiration task running at the same time as a RELEASE request**

### Approach

- All transitions (`fund`, `release`, `refund`, `expire`) are executed within
  **database transactions** (`transaction.atomic`).
- API actions (`fund`, `release`, `refund` in `EscrowViewSet`) use
  `Escrow.objects.select_for_update().get(pk=pk)` to obtain **row-level locks**.
- The Celery expiration task also uses `select_for_update` on each candidate escrow.
- Before every transition, the **current state** is checked; if it has already changed,
  the operation becomes a no-op or raises a clear error.

### Example: RELEASE vs REFUND

- Both actions target `FUNDED` escrows.
- With row-level locking, only one transaction can hold the lock and successfully
  transition the escrow from `FUNDED` to its next state (`RELEASED` or `REFUNDED`).
- The second transaction, once it acquires the lock and reloads the row, sees a
  **non-FUNDED** state and fails with a `ValueError`, resulting in a 400 response.

### Example: RELEASE vs EXPIRATION TASK

- Celery task locks the row and moves `FUNDED → EXPIRED`.
- A concurrent RELEASE API call:
  - Either sees `FUNDED` first and wins (transitioning to `RELEASED`),
  - Or runs after expiration and finds `EXPIRED`, then fails with a 400.
- In either case, the escrow ends up in exactly **one** terminal state.

Tests like `EscrowExpirationTaskTests.test_race_condition_release_vs_expire_is_consistent`
illustrate this behavior.

---

## Running with Docker (recommended for quick start)

This repository includes a `Dockerfile` and `docker-compose.yml` that bring up:

- Django app (`web`)
- MySQL (`db`)
- Redis (`redis`)
- Celery worker (`celery-worker`)
- Celery beat scheduler (`celery-beat`)

### 1. Build and start the stack

From the project root:

```bash
docker-compose up --build
```

The first run will:

- Build the Django image
- Start MySQL, Redis, the web app, Celery worker, and Celery beat

### 2. Apply migrations inside the web container

Open a separate terminal and run:

```bash
docker-compose run --rm web python manage.py migrate
```

### Running tests with Docker Compose (recommended for deterministic DB behavior)

1. Start the DB and Redis services:

```bash
docker compose up -d db redis
```

2. Run the test suite inside the `web` service (this uses the real MySQL instance):

```bash
docker compose run --rm web python manage.py test
```

Notes:
- You can control the test database name with the `DB_TEST_NAME` environment variable (defaults to `test_escrow_service` in `docker-compose.yml`).
- The container entrypoint applies migrations and retries until the DB is reachable, so tests should run reliably once the DB is up.

### Helper commands (Makefile)

This repository includes a `Makefile` with common convenience commands:

- `make up` — start the full compose stack (web, db, redis, celery)
- `make migrate` — run migrations in the `web` container
- `make test` — run the Django test suite inside the `web` container
- `make down` — stop the stack

Example:

```bash
make up
make migrate
make test
```

### CI

This project includes a GitHub Actions workflow at `.github/workflows/ci.yml` that
spins up MySQL and Redis services, runs migrations, and executes the test suite.

### Environment files

This project supports stage-specific environment files using `python-dotenv`.
By default the settings loader reads the file corresponding to the `DJANGO_ENV`
environment variable (defaults to `development`). For example:

- `DJANGO_ENV=test` will load `test.env`
- `DJANGO_ENV=production` will load `production.env`
- fallback to a generic `.env` if stage-specific file is not present

#### Getting started with environment variables

1. Copy the template file to your local `.env`:

```bash
cp sample.env .env
```

2. Edit `.env` with your local database and service credentials:

```bash
nano .env
# or use your preferred editor
```

3. Source the environment or use Docker to run the app (Docker handles env files automatically).

#### Example files included in the repository

- `sample.env` — complete template with all configurable variables (safe to commit).
- `.env`, `test.env`, and `production.env` — stage-specific examples (do NOT commit real secrets).

Do NOT commit real secrets — put sensitive values in a secure secrets store
and keep these files out of version control (they are listed in `.gitignore`).

To run locally with a stage file set `DJANGO_ENV` before running the container:

```bash
export DJANGO_ENV=test
make migrate
make test
```

## Local development: using a Python virtual environment (optional)

Using a virtual environment keeps your local Python packages isolated from system
packages and other projects. This is optional if you always run the app inside
Docker, but recommended for local development and editor tooling.

Create and activate a `venv` then install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Common developer commands (with `venv` active):

```bash
python manage.py runserver
python manage.py migrate
python manage.py test
```

The repository already includes Docker-based workflows; use Docker when you
want an environment matching CI and production (recommended for running tests
that rely on MySQL/Redis). Add `.venv/` to your local `.gitignore` (already
configured) so the virtual environment is not committed.



### 3. Access the services

- API: `http://localhost:8000/api/`
- Swagger UI: `http://localhost:8000/swagger/`

Celery worker and beat will use Redis as broker/backend and periodically
expire eligible escrows via the `expire_funded_escrows` task.

---

## Example Requests

### Create Escrow (Buyer)

```http
POST /api/escrows/
X-User-Id: buyer-123
X-User-Role: buyer
Content-Type: application/json

{
  "seller_id": "seller-456",
  "amount": "100.00",
  "currency": "USD"
}
```

### Fund / Release / Refund

```http
POST /api/escrows/{id}/fund/
X-User-Id: buyer-123
X-User-Role: buyer

POST /api/escrows/{id}/release/
X-User-Id: buyer-123
X-User-Role: buyer

POST /api/escrows/{id}/refund/
X-User-Id: buyer-123
X-User-Role: buyer
```

### Seller Listing Assigned Escrows

```http
GET /api/escrows/
X-User-Id: seller-456
X-User-Role: seller
```

---

## Tests

Tests are in `escrow/tests.py` and can be run with:

```bash
python manage.py test
```

> Note: tests use the MySQL database configured in `escrow_service/settings.py`. Make sure
> MySQL is running and that user `escrow` has privileges on both `escrow_service` and
> `test_escrow_service`.

### Running tests in Docker

You can also run the test suite inside the Docker environment.

1. Make sure the database and Redis are up:

```bash
docker compose up -d db redis
```

2. Run tests from the `web` service:

```bash
docker compose run --rm web python manage.py test
```

3. When finished:

```bash
docker compose down
```

They cover:

- **State transitions**
  - Valid and invalid transitions in the `Escrow` model.
- **Authorization rules**
  - Buyers vs sellers, ownership checks, and restricted actions.
- **Expiration logic**
  - Escrows in `FUNDED` state with past `expires_at` are moved to `EXPIRED`.
  - The Celery task is idempotent.
- **Race-condition scenario**
  - A scenario where expiration and release compete for the same escrow, ensuring
    that at most one terminal state is applied and that API calls fail cleanly
    if the escrow is already expired.

