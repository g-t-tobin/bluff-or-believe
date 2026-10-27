# Read-Only AWS Production Prerequisite Verification

**Outcome: verification blocked; deployment gate is closed.**

This report records only local source/configuration evidence and safe presence
checks. The AWS CLI is unavailable in this environment. AWS credential/profile
environment variables were absent, although local AWS config and credentials
files exist. Their contents were not read. No identity could be established,
so no AWS resource inspection was attempted.

The local process environment did not contain `ROOM_SESSION_SECRET_ARN`,
`CDK_DEFAULT_ACCOUNT`, `CDK_DEFAULT_REGION`, `AWS_REGION`, or
`AWS_DEFAULT_REGION`. No secret value was read or displayed.

## Verification results

| Item | Expected configuration | Evidence obtained | Verification status | Required action |
|---|---|---|---|---|
| AWS identity/account | Account `851725545966`; calling identity must first be verified | AWS CLI unavailable; no AWS identity command could be run. AWS config/credentials files exist, but were not inspected. | **Not verified** | Authorized operator must establish identity using the approved AWS CLI and confirm account `851725545966` before any resource inspection. If it differs, stop. |
| AWS region | `eu-west-1`; frontend certificate checks are in `us-east-1` | Source sets the CDK stack region to `eu-west-1`. Region variables are absent from this process environment. | **Not verified** | Authorized operator must explicitly set/confirm the target region and use `eu-west-1` for regional resources. |
| Session secret ARN | `ROOM_SESSION_SECRET_ARN`, complete Secrets Manager ARN in `eu-west-1`, expected account `851725545966` | Variable absent from local environment. Source requires a complete ARN matching the region and format; project checklist documents the requirement but no concrete ARN is supplied. | **Missing** | Authorized operator must identify an existing application secret or provision one through the approved Secrets Manager process. Do not guess its ARN. |
| Secret metadata/existence | Intended application secret in Secrets Manager, `eu-west-1` | Not queried because AWS CLI is unavailable and identity is unverified. | **Not verified** | After identity verification, authorized operator may inspect metadata only; never retrieve the secret value for this review. |
| Secret value length/randomness | At least 32 bytes of cryptographically random material | Metadata cannot establish this; value was not retrieved. | **Not verified** | Secret owner must verify through an approved secure process that does not disclose or log the value. |
| Secret stability | Same value available across backend tasks and replacements | No live secret or task configuration inspected. Source documents that stable value is required for deterministic session recovery. | **Not verified** | Secret owner and deployment operator must confirm lifecycle/rotation policy preserves the value across replacements, or coordinate any rotation with session migration. |
| ECS secret injection | `ROOM_SESSION_SECRET` injected from `ROOM_SESSION_SECRET_ARN` using Secrets Manager integration | CDK source conditionally configures `ecs.Secret.from_secrets_manager` when the ARN is supplied. No production template or live task definition was inspected. | **Source configured; live injection Not verified** | Inspect the reviewed production template/task definition after a real ARN is provided. Confirm secret value is not embedded in plaintext. |
| ECS execution-role access to secret | Execution role can retrieve the configured secret; KMS/resource policies permit retrieval if applicable | Source delegates ECS secret handling to the CDK ECS secret integration. No synthesized production policy, live execution role, resource policy, permission boundary, SCP, or KMS key policy was inspected. | **Not verified** | Authorized operator must inspect the execution-role grants and relevant secret/KMS policies. Policy presence alone does not establish effective access. |
| ECS task role DynamoDB access | `GetItem` and `PutItem` on this table only | CDK source adds task-role policy actions `dynamodb:GetItem` and `dynamodb:PutItem` scoped to the table ARN. No live role or effective access was inspected. | **Source configured; live/effective access Not verified** | Inspect the production policy and relevant permission boundaries, SCPs, table resource policies, and explicit denies. Confirm no unnecessary wildcard access. |
| DynamoDB table | `BluffOrBelieveRooms`; String partition key `room_id`; on-demand billing; AWS-managed encryption; TTL `expires_at`; PITR disabled; retained | These properties and `RemovalPolicy.RETAIN` are configured in CDK source. No live table was inspected. | **Source configured; live resource Not verified** | Authorized operator must inspect the table’s live settings and retention/deletion protection expectations before rollout. |
| Frontend certificate | ARN `arn:aws:acm:us-east-1:851725545966:certificate/ee586878-a05c-463c-a4ac-f13ab9d17e9b`; CloudFront region `us-east-1` | Expected ARN is in source validation. ACM was not queried. | **Not verified** | In the intended account, inspect ACM metadata in `us-east-1`: ARN, status, covered domains, expiration, and renewal state. |
| API certificate | ARN `arn:aws:acm:eu-west-1:851725545966:certificate/97548b1e-afa4-42c3-8cf0-a0fa57e3edd9` | Expected ARN is in source validation. ACM was not queried. | **Not verified** | In the intended account, inspect ACM metadata in `eu-west-1`: ARN, status, covered domains, expiration, and renewal state. |
| Frontend domain and DNS | `play.ai-autofy-general-server.com` points to the intended CloudFront distribution | CDK source configures the frontend custom domain and emits the generated distribution domain as an output. No hosted zone or DNS was inspected. | **Not verified** | Authorized DNS operator must identify the correct hosted zone and verify the record target against the deployed distribution output. |
| API domain and DNS | `api.ai-autofy-general-server.com` points to the intended ALB | CDK source configures the API domain as the CloudFront API origin and emits the ALB DNS name as an output. No hosted zone or DNS was inspected. | **Not verified** | Authorized DNS operator must verify the record target against the actual ALB output and certificate coverage. |
| CORS/frontend origin | `https://play.ai-autofy-general-server.com`, exact HTTPS origin; no local origins | Configuration source requires the exact documented origin and rejects localhost, HTTP, paths, and mismatches. Production environment was not present and no production template was synthesized. | **Source validation confirmed; live configuration Not verified** | Use the validated production settings and inspect the production task definition before release. |
| Frontend/API path routing | Frontend uses `/api`; CloudFront forwards `/api/*` to HTTPS API origin and strips `/api` | Frontend `.env.production` specifies `/api`; CDK source defines the API behavior and rewrite. No deployed distribution was inspected. | **Source configured; live routing Not verified** | Verify behavior against the reviewed production template and approved post-deployment checks. Do not make production requests as part of this report. |
| ECS cluster/service/task definition | App’s ECS Fargate service, production environment, table name and secret injection | CDK source defines a Fargate service and task container with `APP_ENV=production`, `AWS_REGION`, `ROOMS_TABLE_NAME`, and conditional secret injection. No live ECS resources or production template were inspected. | **Source configured; live resources Not verified** | Authorized operator must confirm the intended cluster/service/task definition and rollout target in the intended account/region. |
| ALB health/readiness | ALB health check `/health`; `/ready` is not the routing gate | CDK source configures `/health`; project documentation defines `/ready` for manual deployment verification. | **Source confirmed; live ALB Not verified** | Confirm health check in the reviewed deployment configuration; include `/ready` in authorized verification, not as an automatic routing gate. |

## Access limitations and evidence boundary

- AWS CLI was not available (`aws` executable not found).
- AWS credential/profile environment variables and region variables were absent.
- Local AWS config and credentials files were present, but were not read. Their
  presence does not establish an authorized identity or target account.
- No AWS identity check, Secrets Manager metadata query, ACM query, Route 53
  query, DynamoDB inspection, ECS inspection, or IAM inspection was run.
- No production synthesis, CDK diff, production request, deployment, bootstrap,
  or resource-changing operation was performed.
- CDK source shows intended settings, but source inspection cannot establish
  live resources, effective IAM access, certificate validity, DNS correctness,
  or secret contents/stability.

## Remaining prerequisites before production synthesis

1. An authorized operator must establish and confirm the intended AWS identity
   and account `851725545966`, then explicitly confirm `eu-west-1`.
2. Provide the actual `ROOM_SESSION_SECRET_ARN` through the approved deployment
   configuration. Verify secret metadata and ECS execution-role/KMS access
   without retrieving the value.
3. Securely verify the secret’s minimum length, cryptographic quality, and
   stability across tasks and replacements without disclosing it.
4. Verify the expected frontend and API certificates in their respective ACM
   regions, including status, domain coverage, expiry, and renewal.
5. Verify the intended Route 53 records against the actual CloudFront and ALB
   outputs.
6. Verify the live DynamoDB table and ECS resources, and review effective
   permissions including boundaries, SCPs, resource policies, KMS policies,
   and explicit denies.
7. Only then populate the validated production settings and conduct a separate
   explicitly authorized production synthesis/template review. This report does
   not authorize synthesis or deployment.

## Deployment gate

**HOLD — production synthesis and deployment prerequisites are not verified.**
The missing secret ARN alone blocks production configuration validation. AWS
identity is also unestablished, so every live AWS resource and permission check
remains **Not verified**. Do not deploy until the authorized operator completes
the prerequisite checks and a separate approval is given.
