# Bluff or Believe: The Big Fib

Local project foundation for the approved multiplayer party game.

## Scope

This project is a local multiplayer prototype that is now prepared for a single-task AWS beta deployment without adding Redis or multi-process state sharing. The local development flow remains intact, and the production configuration is controlled by environment variables.

- React + TypeScript + Vite frontend
- FastAPI backend with WebSockets
- DynamoDB-backed persistent room state in production, with in-memory storage for local development and tests
- Docker Compose for the local backend container
- Health endpoint and automated backend tests
- Production prep for environment-driven URLs, CORS and container deployment
- Minimal CDK stack for a single ECS Fargate beta in eu-west-1

## Prerequisites

- Python 3.11+
- Node.js 20+
- npm
- Docker Engine or Docker Desktop (optional for local Redis and image validation)
- AWS account and a valid domain/certificate only when preparing a hosted deployment

## Environment variables

Create a local `.env` file from the repository root using the template in `.env.example`.

Frontend variables:

```bash
VITE_API_BASE_URL=http://localhost:8000
VITE_WS_BASE_URL=ws://localhost:8000
```

Production values for the registered domain:

```bash
VITE_API_BASE_URL=/api
```

Leave `VITE_WS_BASE_URL` unset in production. The frontend derives a `wss://` URL from its current HTTPS origin and uses the same `/api` CloudFront proxy for WebSockets.

Production frontend builds require `VITE_API_BASE_URL=/api` and no external `VITE_WS_BASE_URL`; local development retains the `http://localhost:8000` and `ws://localhost:8000` defaults.

Backend variables:

```bash
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
HOST=0.0.0.0
PORT=8000
```

Production value for the frontend custom domain:

```bash
CORS_ORIGINS=https://play.ai-autofy-general-server.com
```

Optional AWS deployment variables for HTTPS and custom domains:

```bash
API_DOMAIN_NAME=api.ai-autofy-general-server.com
API_CERTIFICATE_ARN=arn:aws:acm:eu-west-1:851725545966:certificate/97548b1e-afa4-42c3-8cf0-a0fa57e3edd9
FRONTEND_DOMAIN_NAME=play.ai-autofy-general-server.com
FRONTEND_CERTIFICATE_ARN=arn:aws:acm:us-east-1:851725545966:certificate/ee586878-a05c-463c-a4ac-f13ab9d17e9b
```

Do not commit real credentials, AWS account IDs, secret values or tokens. The default values remain suitable for local development when these variables are unset.

## Run locally

### Start backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Health check: http://127.0.0.1:8000/health
API docs: http://127.0.0.1:8000/docs

Run backend tests with `python -m pytest -q` from `backend/`.

### Start frontend

In another terminal:

```bash
cd frontend
npm install
npm run dev
```

Open the URL printed by Vite, normally http://localhost:5173.

Run frontend checks with `npm run lint` and `npm run build`.

### Local Docker build

The backend image can be built from the backend directory:

```bash
cd backend
docker build -t bluff-or-believe-backend .
docker run --rm -p 8000:8000 bluff-or-believe-backend
```

The container exposes the same health endpoint used by AWS ALB target checks.

## Production readiness notes

- The frontend uses `VITE_API_BASE_URL` and `VITE_WS_BASE_URL` so the same build can target local or hosted environments.
- Players receive a room-scoped, 30-day session in an HttpOnly, Secure, SameSite=Lax cookie. Production HTTP and WebSocket requests use the frontend's same-origin `/api` CloudFront behavior, so the cookie remains first-party and is not exposed to JavaScript or URLs.
- The CDK distribution forwards `/api/*` to the HTTPS API hostname, forwards cookies and request headers without caching, and strips the `/api` prefix at the edge. Production frontend builds require `VITE_API_BASE_URL=/api`; WebSockets derive `wss://` from the current HTTPS frontend host.
- State-changing HTTP requests and WebSocket handshakes validate the exact frontend Origin. The API domain remains available separately for health checks and non-browser clients.
- A player name is not an identity credential. Rejoining an existing name requires that room's existing session cookie; room codes are high-entropy UUIDs and are required to join.
- Room creation requires a UUID `Idempotency-Key`; clients must reuse that key when retrying an uncertain creation result. The frontend persists its pending creation key locally until the room response is confirmed.
- Player joins use a separate room-and-name-bound idempotency key. The server uses a stable Secrets Manager-provided secret to derive a recoverable session credential; only its digest and the join-key digest are stored with room state. The browser stores the join operation key for retry, never the session token.
- The backend CORS policy is configured from `CORS_ORIGINS`, with local defaults kept for laptop development.
- The backend container listens on `HOST` and `PORT`, defaulting to `0.0.0.0:8000` for deployment.
- The `backend/Dockerfile` includes a simple HTTP health check at `/health` for container orchestration.
- Production room state is stored in the retained `BluffOrBelieveRooms` DynamoDB table and survives backend task replacement. Local development and tests use in-memory storage. WebSocket connections remain process-local and players reconnect manually after a task replacement.
- Rooms expire 24 hours after successful room-state changes or an authenticated WebSocket reconnect. Reads, polling, chat messages and disconnects do not extend expiry. `/health` is a liveness check; `/ready` performs one storage read and returns 503 when persistence is unavailable.
- The first deployment that replaces the previous memory-only backend will lose its existing in-memory rooms. Affected players must create or join rooms again; there is no migration source for those rooms.

## AWS deployment prerequisites

Use the [production configuration checklist](./infrastructure/cdk/PRODUCTION_CHECKLIST.md)
to track required values, operator verification, and the missing session-secret
ARN before any future production synthesis or deployment.

Before real deployment, prepare the following:

1. A target AWS account in `eu-west-1`.
2. A valid ACM certificate for the API domain in `eu-west-1`.
3. A valid CloudFront ACM certificate for the frontend custom domain in `us-east-1`.
4. Route53 or equivalent DNS records pointing to the ALB and the CloudFront distribution.
5. Approval to create the required resources and pay the associated charges.
6. For a future approved AWS deployment only, the account may require `cdk bootstrap`. Bootstrap creates AWS resources and may incur charges.

No AWS deployment or bootstrapping is performed in this repository without explicit approval.

## Final deployment configuration

Use the live production values for the registered domain and issued ACM certificates.

```bash
VITE_API_BASE_URL=/api
CORS_ORIGINS=https://play.ai-autofy-general-server.com
API_DOMAIN_NAME=api.ai-autofy-general-server.com
API_CERTIFICATE_ARN=arn:aws:acm:eu-west-1:851725545966:certificate/97548b1e-afa4-42c3-8cf0-a0fa57e3edd9
FRONTEND_DOMAIN_NAME=play.ai-autofy-general-server.com
FRONTEND_CERTIFICATE_ARN=arn:aws:acm:us-east-1:851725545966:certificate/ee586878-a05c-463c-a4ac-f13ab9d17e9b
ROOM_SESSION_SECRET_ARN=<existing Secrets Manager secret ARN in eu-west-1>
```

The Secrets Manager ARN is a placeholder for an explicitly provisioned stable secret containing at least 32 cryptographically random bytes. Never put its value in source code or deployment templates.

## Local CDK workflow

The CDK app lives in `infrastructure/cdk`.

```bash
cd infrastructure/cdk
python -m pip install -r requirements.txt
APP_ENV=local cdk synth
```

The command above is for local synthesis only and does not deploy resources. CDK execution requires an explicit `APP_ENV=local` or `APP_ENV=production`; production mode additionally requires every validated production value, including the actual pre-provisioned Secrets Manager ARN. Set and verify those values before a future production synthesis.

## Future AWS deployment and rollback (requires explicit approval)

The following are **future deployment commands only**. They have not been run as part of local implementation or review. Verify the AWS identity and review the CDK diff before applying changes:

```bash
aws sts get-caller-identity
cdk diff BluffOrBelieveStack
cdk deploy BluffOrBelieveStack
```

If the target account has not been bootstrapped, the optional future command is:

```bash
cdk bootstrap aws://851725545966/eu-west-1
```

Bootstrap creates AWS resources and may incur charges; it also requires explicit approval.

The stack intentionally keeps a single ECS Fargate task behind a public ALB and does not add Redis or any shared session store. The frontend uses S3 + CloudFront, and the backend image is built from the local `backend` directory via `ecs.ContainerImage.from_asset("../../backend")`.

This is the simplest valid asset workflow for a single-task beta. CDK handles the image build and ECR asset publishing during deployment, so a manual ECR push is not required for the initial stack.

## DNS and certificate validation workflow

1. Request and validate an ACM certificate for the API custom domain in `eu-west-1`.
2. Request and validate a separate ACM certificate for the frontend custom domain in `us-east-1` for CloudFront.
3. Create Route53 records or equivalent DNS aliases:
   - `play.ai-autofy-general-server.com` -> CloudFront distribution
   - `api.ai-autofy-general-server.com` -> ALB
4. Confirm both domains resolve over HTTPS before the first production release.

## Deployment verification

After deployment, verify the following manually before releasing public traffic:

```bash
curl -I https://play.ai-autofy-general-server.com
curl -I https://api.ai-autofy-general-server.com/health
curl -I https://api.ai-autofy-general-server.com/docs
```

Also confirm that the browser can open the game on `https://play.ai-autofy-general-server.com` and that the frontend connects to the same-origin `wss://<frontend-host>/api/ws/rooms/{room_id}` endpoint successfully.

## Rollback plan

If a future production deployment fails:

1. Keep the last known-good CloudFormation stack or deployment artifact.
2. Redeploy the previous frontend assets to the S3 bucket and invalidate CloudFront cache.
3. Roll back to a known-good **DynamoDB-capable** backend version compatible with the retained table and schema. Keep the table retained throughout rollback.
4. Do not use the previous memory-only backend as a rollback target: it cannot read persisted rooms, so those rooms become unavailable. Existing in-memory rooms from before the first persistence rollout are lost and affected players must rejoin.
5. Confirm `/health`, authenticated room restoration, and manual WebSocket reconnection before re-enabling traffic.

## Codex workflow

Ask Codex to work on one scoped task at a time. Require it to inspect files, explain its plan, add tests, run tests, and report exact results. Do not allow AWS provisioning or deployment commands without separate approval.

Never commit credentials, `.env` files, session tokens, or AWS access keys.
