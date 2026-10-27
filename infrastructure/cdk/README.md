# AWS infrastructure

This directory contains a minimal AWS CDK app for a small public beta in `eu-west-1`.

CDK execution requires an explicit `APP_ENV=local` or `APP_ENV=production`. Local synthesis uses the local CORS defaults and does not represent a production template:

```bash
APP_ENV=local cdk synth
```

Production synthesis additionally requires all validated production values, including the actual pre-provisioned Secrets Manager ARN. Never use a placeholder ARN or local CORS configuration for production synthesis.

Track production inputs and operator checks in [PRODUCTION_CHECKLIST.md](./PRODUCTION_CHECKLIST.md). It distinguishes source-defined expectations from live AWS resources, which remain unverified until an authorized operator checks them.

What it includes:
- S3 bucket for the frontend static site
- CloudFront distribution with optional custom domains and HTTPS
- One ECS Fargate backend task behind an internet-facing ALB
- DynamoDB on-demand table for durable room state
- HTTP-to-HTTPS redirect on the ALB when a certificate is configured
- CloudWatch log group for backend logs
- Container liveness check on `/health`; persistence readiness is available at `/ready`
- Optional custom-domain and certificate configuration for the frontend and API

Important limitations:
- Room state is persisted in the retained `BluffOrBelieveRooms` table and survives task restarts.
- Rooms expire 24 hours after successful room-state changes or an authenticated WebSocket reconnect. Reads, polling, chat messages and disconnects do not extend expiry.
- `/health` is a liveness check. `/ready` makes one repository read and returns 503 if persistence is unavailable; it does not write or retry. The ALB continues to use `/health` so a transient storage outage does not trigger task-replacement churn; include `/ready` in deployment verification.
- WebSocket connections remain process-local and are lost when a task restarts. Players can reconnect manually; automatic reconnect is not enabled.
- The stack keeps one backend task because WebSocket fan-out is process-local. DynamoDB optimistic version checks prevent conflicting room writes but do not distribute WebSocket broadcasts.
- The rooms table uses on-demand billing, AWS-managed encryption, and the `expires_at` TTL attribute. Application reads reject expired rooms before asynchronous TTL deletion. Point-in-time recovery is disabled initially; the table is retained when the stack is removed.
- The ECS task role is granted only `dynamodb:GetItem` and `dynamodb:PutItem` on this table.
- Join-session recovery derives a session token using a stable secret injected from AWS Secrets Manager. The secret value is never part of the synthesized template or room records. Production configuration must provide `ROOM_SESSION_SECRET_ARN` for a pre-created secret in `eu-west-1`; its value must contain at least 32 bytes of cryptographically random material.
- A join idempotency key is bound to the exact room and player name. Its digest is persisted with the player, while the recoverable session token is deterministically derived using the server-side secret. A key alone cannot derive or authenticate the session.
- A timed-out DynamoDB write is reconciled using a strongly consistent read and a transition receipt stored with the room. If the receipt cannot establish the outcome, the backend reports an uncertain result and the client must refresh before deciding whether another action is needed.
- Local CDK synthesis keeps localhost CORS defaults. Production synthesis requires explicit, validated frontend and CORS origins; the frontend origin must be HTTPS, match `FRONTEND_DOMAIN_NAME`, and be included in `CORS_ORIGINS`. The generated CloudFront distribution hostname is added from the CDK construct rather than hard-coded.

Prerequisites before deployment:
1. Install Python CDK dependencies from `infrastructure/cdk/requirements.txt`.
2. Set the AWS account credentials for the target environment.
3. Prepare or request an ACM certificate for the API domain in `eu-west-1`.
4. Prepare or request a CloudFront ACM certificate for a custom frontend domain in `us-east-1`.
5. Set domain and certificate environment variables before deployment if HTTPS is enabled.

Example environment variables for the target deployment using the registered domain and issued ACM certificates:

```bash
export APP_ENV="production"
export REQUIRE_PRODUCTION_CONFIG="true"
export CDK_DEFAULT_ACCOUNT="851725545966"
export CDK_DEFAULT_REGION="eu-west-1"
export AWS_DEFAULT_REGION="eu-west-1"
export API_DOMAIN_NAME="api.ai-autofy-general-server.com"
export API_CERTIFICATE_ARN="arn:aws:acm:eu-west-1:851725545966:certificate/97548b1e-afa4-42c3-8cf0-a0fa57e3edd9"
export FRONTEND_DOMAIN_NAME="play.ai-autofy-general-server.com"
export FRONTEND_CERTIFICATE_ARN="arn:aws:acm:us-east-1:851725545966:certificate/ee586878-a05c-463c-a4ac-f13ab9d17e9b"
export FRONTEND_ORIGIN="https://play.ai-autofy-general-server.com"
export CORS_ORIGINS="https://play.ai-autofy-general-server.com"
export ROOM_SESSION_SECRET_ARN="<existing Secrets Manager secret ARN in eu-west-1>"
```

`ROOM_SESSION_SECRET_ARN` is intentionally an explicit placeholder: create and securely provision a stable secret with at least 32 random bytes before an approved deployment. The CDK task definition injects the value using ECS Secrets Manager integration; do not put the secret value in shell arguments, source files, or synthesized configuration.

When `APP_ENV=production` or `REQUIRE_PRODUCTION_CONFIG=true`, the CDK app validates that these values are present and exactly match the expected deployment configuration. This prevents stale values or old domain/certificate values from being synthesized into the template. The CloudFront certificate must be in `us-east-1`, while the ALB certificate must be in `eu-west-1`.

For a future, explicitly approved production deployment, after setting and verifying all documented production values, run the template validation script to confirm the synthesized output contains the intended aliases, certificates, table and task configuration:

```bash
cdk synth
python validate_production_template.py
```

The final deployment requires DNS validation for both domains before traffic can complete HTTPS. The route for `play.ai-autofy-general-server.com` points to the CloudFront distribution, and the route for `api.ai-autofy-general-server.com` points to the ALB.

## Pre-deployment and rollback procedure

The commands and steps in this section describe a **future deployment only**. They have not been executed. Do not run deployment or bootstrap commands without separate approval.

Before the first persistence-enabled deployment:

1. Build and test the exact backend image and frontend build intended for release; run backend tests and Ruff, frontend tests, lint and production build, then synthesize and inspect the CDK template.
2. Confirm `ROOMS_TABLE_NAME` is present in the synthesized ECS task definition and the task role is scoped to the retained rooms table.
3. Schedule the rollout and notify players. The previous backend kept rooms only in task memory: existing in-memory rooms are lost when that task is replaced on the first rollout. There is no source from which to migrate those rooms, and affected players must create or join rooms again.
4. Keep the table retained. Do not remove it or run a memory-only application version as a way to restore service.

Review the future deployment diff before applying it. From `infrastructure/cdk`, after setting the production values above:

```bash
# Future commands only; require explicit deployment approval.
cdk synth BluffOrBelieveStack
python validate_production_template.py
cdk diff BluffOrBelieveStack
cdk deploy BluffOrBelieveStack
```

The CDK stack builds and publishes the backend container asset as part of deployment. The repository does not define a frontend S3 publication command; use the approved release process and the actual bucket/distribution outputs rather than guessing resource names.

After deployment, the future verification should include `/health`, authenticated room creation/retrieval, a task replacement followed by restoration from DynamoDB, and manual WebSocket reconnection with the existing session cookie.

For rollback, prefer redeploying a known-good **DynamoDB-capable** backend version that uses the same table and compatible room schema. The previous memory-only backend cannot read or restore DynamoDB rooms, so rolling back to it makes the new persistent rooms unavailable even though the retained table still exists. Keep the persistence-capable task definition/image available and validate schema compatibility before rollout. If an incompatible release has already written records, restore service with a compatible reader rather than deleting or replacing the table. WebSocket connections are transient across task replacement; players can use the manual Refresh / reconnect control.

Typical DNS setup (Route53 or equivalent):
- `play.ai-autofy-general-server.com` -> CloudFront alias (A/AAAA)
- `api.ai-autofy-general-server.com` -> ALB alias (A/AAAA)
- ACM DNS validation records created for the certificate domains and validated before the stack is deployed.

Cost estimate (planning only, not a guaranteed AWS quote):
- S3 + CloudFront for a small static frontend: roughly €2-10/month.
- ALB: roughly €20-35/month in eu-west-1 for a small, always-on internet-facing ALB.
- ECS Fargate: roughly €8-25/month for a single small task (for example 0.25 vCPU / 0.5 GiB to 0.5 vCPU / 1 GiB, always-on).
- CloudWatch logs and metrics: roughly €2-8/month.
- DynamoDB on-demand room storage: likely under a few euros per month for low-volume rooms; request volume, item size, backups and regional rates affect the total.
- ACM certificate and Route53 records: roughly €1-3/month total if a custom domain is used.
- Public IPv4 charges: usually no separate public IPv4 fee for the ALB itself, but extra public resources or NAT components can add several euros per month.
- Total rough range: roughly €35-80/month for a small, single-task beta with HTTPS and a custom domain.

This estimate assumes one always-on Fargate task, one ALB, a low-traffic static frontend, and a small DynamoDB table.

Deployment note:
- The first deployment may require `cdk bootstrap`.
- `cdk bootstrap` creates AWS bootstrap resources and can lead to billable charges in the target account.
- No bootstrap or deployment is executed in this repository without explicit approval.

Do not run `cdk bootstrap`, `cdk deploy`, or other commands that create or change AWS resources without separate approval.

The existing `cdk.out/` directory is generated output and may reflect earlier/local configuration. Do not treat it as a production artifact; regenerate and validate under the explicitly documented production configuration before a future approved deployment.
