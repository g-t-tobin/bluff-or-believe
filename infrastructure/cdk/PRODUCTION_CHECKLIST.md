# Production Configuration Checklist

This checklist records source-defined production settings and the evidence an
authorized operator must collect before a future deployment. Values in source
code and this document are configuration expectations only; they do not prove
that an AWS account, certificate, DNS record, secret, or other live resource
exists or is usable.

**Current status: production configuration is incomplete.** The actual
`ROOM_SESSION_SECRET_ARN` has not been supplied. All live AWS resource checks
below are **Not verified**. Do not synthesize or deploy production settings
until the missing ARN is securely provided and all prerequisites are checked.

## Required configuration and verification

| Configuration item | Environment variable or CDK setting | Expected value or format | Where to obtain it | How an authorised operator can verify it | Verified status |
|---|---|---|---|---|---|
| CDK mode | `APP_ENV` | `production` | Deployment environment configuration | Confirm the deployment job or approved operator environment explicitly sets production mode. | Source requirement confirmed; production environment **Not verified** |
| Strict production guard | `REQUIRE_PRODUCTION_CONFIG` | `true` is recommended. `APP_ENV=production` already invokes validation; this flag is not independently required in production. | Deployment environment configuration | Confirm the guard is enabled and do not use `APP_ENV=local` for production synthesis. | Source behavior confirmed; production environment **Not verified** |
| AWS account | `CDK_DEFAULT_ACCOUNT` | `851725545966` | Authorized AWS account owner or account inventory | In the AWS Management Console, confirm the signed-in account ID and intended deployment account. Do not infer identity from this source value. | Expected value is source-defined; live account **Not verified** |
| CDK deployment region | `CDK_DEFAULT_REGION` | `eu-west-1` | Deployment environment configuration | Confirm the intended deployment region with the account owner. | Expected value is source-defined; live region **Not verified** |
| CDK/AWS default region | `AWS_DEFAULT_REGION` | `eu-west-1` | Deployment environment configuration | Confirm it agrees with `CDK_DEFAULT_REGION` and the stack region. The CDK app sets the stack region to `eu-west-1`. | Expected value is source-defined; live region **Not verified** |
| Frontend domain | `FRONTEND_DOMAIN_NAME` | `play.ai-autofy-general-server.com` | Approved domain/DNS owner | Confirm domain ownership and the planned CloudFront alias with the DNS owner. | Expected value is source-defined; domain/DNS **Not verified** |
| API domain | `API_DOMAIN_NAME` | `api.ai-autofy-general-server.com` | Approved domain/DNS owner | Confirm the planned ALB hostname and DNS routing with the DNS owner. | Expected value is source-defined; domain/DNS **Not verified** |
| CloudFront certificate | `FRONTEND_CERTIFICATE_ARN` | `arn:aws:acm:us-east-1:851725545966:certificate/ee586878-a05c-463c-a4ac-f13ab9d17e9b` | Authorized certificate administrator | In ACM in `us-east-1`, inspect certificate metadata. Confirm its ARN, status, domain coverage, and account. Do not use the ARN in a deployment until verified. | Expected value is source-defined; certificate **Not verified** |
| API/ALB certificate | `API_CERTIFICATE_ARN` | `arn:aws:acm:eu-west-1:851725545966:certificate/97548b1e-afa4-42c3-8cf0-a0fa57e3edd9` | Authorized certificate administrator | In ACM in `eu-west-1`, inspect certificate metadata. Confirm its ARN, status, domain coverage, and account. | Expected value is source-defined; certificate **Not verified** |
| Frontend HTTPS origin | `FRONTEND_ORIGIN` | `https://play.ai-autofy-general-server.com` | Derived from the approved frontend domain and HTTPS design | Check it is exactly HTTPS plus `FRONTEND_DOMAIN_NAME`, with no path, trailing slash, query, or fragment. | Expected value is source-defined; deployment configuration **Not verified** |
| CORS allowlist | `CORS_ORIGINS` | `https://play.ai-autofy-general-server.com` | Approved frontend origin | Confirm the exact frontend origin is included and no localhost, HTTP, or unintended origins are present. The stack also adds the generated CloudFront distribution hostname when a custom frontend domain is configured. | Expected value is source-defined; deployed CORS **Not verified** |
| Secrets Manager secret identifier | `ROOM_SESSION_SECRET_ARN` | Actual complete Secrets Manager ARN in `eu-west-1`, for example format `arn:aws:secretsmanager:eu-west-1:<12-digit-account>:secret:<secret-name>-<6-character-suffix>`. Do not substitute this format example for a real ARN. | Authorized Secrets Manager owner; see [Finding and provisioning the session secret](#finding-and-provisioning-the-session-secret). | In Secrets Manager, select `eu-west-1`; inspect the intended secret’s metadata and ARN, and confirm the account, purpose, and access policy. Never retrieve or print the secret value for this check. | **Missing; Not verified** |
| Runtime session secret value | `ROOM_SESSION_SECRET` | Injected into the ECS container by CDK from `ROOM_SESSION_SECRET_ARN`; at least 32 bytes and stable across tasks, replacements, and restarts. Not a CDK input or source/config-file value. | Stored only in AWS Secrets Manager and supplied through ECS secret injection. | An authorized operator should verify the secret’s provisioning and ECS execution-role access without displaying or exporting its value. | **Not verified** |
| DynamoDB table name | CDK resource setting `table_name`; backend runtime `ROOMS_TABLE_NAME` | `BluffOrBelieveRooms`; ECS receives a reference to the CDK table resource. | Fixed by `infrastructure/cdk/bluff_or_believe_stack.py`. | Inspect the future synthesized template for the table name and ECS environment reference. In AWS, an authorized operator can verify the table in `eu-west-1`. | Source setting confirmed; live table **Not verified** |
| Frontend API base URL | `VITE_API_BASE_URL` | `/api` | Frontend production environment; currently `frontend/.env.production` | Confirm the production build uses the same-origin CloudFront `/api` proxy. | Local file/source value confirmed; deployed frontend **Not verified** |
| Frontend WebSocket base URL | `VITE_WS_BASE_URL` | Unset or empty in production; the frontend derives `wss://` from the page origin and uses `/api`. | Frontend production environment; currently empty in `frontend/.env.production` | Confirm the production build does not use a separate API/WebSocket host. | Local file/source value confirmed; deployed frontend **Not verified** |
| Backend environment and region | ECS task environment `APP_ENV`, `AWS_REGION` | `production`; `eu-west-1` from the CDK stack region | Set by the CDK stack | Inspect the future template’s container environment. | Source setting confirmed; deployed task **Not verified** |
| Backend listener | ECS task environment `HOST`, `PORT` | `0.0.0.0`, `8000` | Set by the CDK stack | Inspect the future template and confirm it matches the container port and health check. | Source setting confirmed; deployed task **Not verified** |
| Destructive bucket opt-in | `ALLOW_DESTRUCTIVE_DESTROY` | Must remain unset or false for production; source defaults to false. | Deployment environment configuration | Confirm the variable is not enabled. The DynamoDB table itself has `RETAIN` independently. | Source default confirmed; deployment environment **Not verified** |
| AWS authorization | CDK credential provider chain | Authorized credentials for the intended account are required for future AWS operations; no credential value belongs in this checklist. | Organization-approved identity process | An authorized operator verifies identity using the organization’s approved process immediately before any separately approved AWS operation. | **Not verified; no AWS identity check performed** |

### Source-defined infrastructure settings

The CDK source configures DynamoDB with partition key `room_id` (String),
on-demand billing, AWS-managed encryption, TTL attribute `expires_at`, point-in-
time recovery disabled, and `RemovalPolicy.RETAIN`. The ECS task role is granted
`dynamodb:GetItem` and `dynamodb:PutItem` on this table. The ALB health check
uses `/health`; `/ready` is available for deployment verification and is not
configured as an ALB routing gate.

These are source-level settings only. Verify the synthesized template and then
the AWS change set during an authorized deployment process.

## Finding and provisioning the session secret

`ROOM_SESSION_SECRET_ARN` and `ROOM_SESSION_SECRET` are different values:

- `ROOM_SESSION_SECRET_ARN` is the identifier for a secret in AWS Secrets
  Manager. CDK receives this identifier and uses it to configure ECS injection.
- `ROOM_SESSION_SECRET` is the secret value read by the backend at runtime. ECS
  injects it into the container; it must not be supplied to CDK as a plaintext
  deployment variable, committed, written into `.env` files, printed, logged,
  or copied into a report or template.

An authorized operator can check whether an appropriate secret already exists:

1. Open AWS Secrets Manager in the AWS Management Console.
2. Select the intended account and region `eu-west-1`.
3. Inspect secret metadata and ARN; do not reveal or copy the secret value.
4. Confirm with the application/security owner that this secret is intended for
   this application and can remain stable across task replacements and backend
   restarts.
5. Confirm the secret is accessible to the ECS task execution role and that any
   applicable resource policy or customer-managed KMS policy permits retrieval.

This project does not establish that a suitable secret exists. If none exists,
an authorized operator must create and securely provision one through the
approved Secrets Manager process before production configuration validation or
synthesis. Use at least 32 bytes of cryptographically random material. Preserve
the same value for every backend task and future replacement; rotating it
without a coordinated session migration invalidates deterministic session
recovery. Record only the ARN in the deployment configuration. Never include the
secret value in a command, source file, local environment file, build output, or
ticket.

## Local validation without AWS access

These checks do not deploy, bootstrap, make production requests, or verify
live AWS resources. Do not use AWS CLI/CDK diff/deploy commands for this
checklist. Do not run production synthesis until the actual secret ARN and all
other required values have been supplied by an authorized operator.

From the repository root, run the source-level tests and checks with existing
installed dependencies:

```bash
PYTHONPATH=infrastructure/cdk python -m pytest -q infrastructure/cdk/test_configuration.py
python -m ruff check infrastructure/cdk/app.py infrastructure/cdk/configuration.py infrastructure/cdk/bluff_or_believe_stack.py infrastructure/cdk/test_configuration.py
python -m compileall -q infrastructure/cdk
```

For local-only synthesis, use explicit local configuration, disable credential
and metadata discovery, and write output outside the project. This output uses
local CORS defaults and must not be treated as a production template:

```bash
cd infrastructure/cdk
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
  -u AWS_PROFILE -u AWS_DEFAULT_PROFILE \
  AWS_EC2_METADATA_DISABLED=true \
  AWS_SHARED_CREDENTIALS_FILE=/dev/null \
  AWS_CONFIG_FILE=/dev/null \
  APP_ENV=local REQUIRE_PRODUCTION_CONFIG=false \
  CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173 \
  CDK_DEFAULT_ACCOUNT=851725545966 CDK_DEFAULT_REGION=eu-west-1 \
  AWS_DEFAULT_REGION=eu-west-1 \
  CDK_OUTDIR=/tmp/bluff-or-believe-local-review \
  JSII_RUNTIME_PACKAGE_CACHE=disabled \
  python app.py
```

The project has a cached CDK availability-zone context for the documented
account and region. If local synthesis reports that it needs a context lookup,
stop; do not enable credentials or retry with AWS access. The local template is
for development only.

## Future production review and rollout gate

Before any separately authorized AWS operation:

1. Resolve every missing configuration item above with the account, domain,
   certificate, and security owners. Keep live-resource status marked
   **Not verified** until those owners verify it.
2. Supply all production settings to the approved deployment environment,
   including the real `ROOM_SESSION_SECRET_ARN`; never supply the secret value
   to CDK.
3. Run production configuration validation and synthesis only after all inputs
   are explicitly supplied. Stop if validation fails or CDK requires a context
   lookup. Review the generated template for table retention, key, TTL,
   billing, encryption, IAM scope, CORS, certificates, and Secrets Manager
   injection. Do not use the local template as a substitute.
4. Have an authorized operator verify the AWS account and region and review the
   eventual CDK change set/diff before applying anything. This checklist does
   not authorize bootstrap, deployment, image publication, or asset upload.
5. Schedule the first rollout with affected players informed: rooms held only
   in the old memory-only backend cannot be migrated and players will need to
   rejoin. Afterward verify `/health`, `/ready`, authenticated room persistence
   across task replacement, and manual WebSocket reconnection.

For rollback, use a known-good DynamoDB-capable backend version compatible with
the persisted room schema. The old memory-only backend cannot read persisted
rooms and is not a safe rollback target once DynamoDB rooms are in use. Keep the
DynamoDB table retained throughout rollback; do not delete or replace it to
restore service.
