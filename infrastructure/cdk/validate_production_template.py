#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

FRONTEND_DOMAIN = "play.ai-autofy-general-server.com"
FRONTEND_ORIGIN = f"https://{FRONTEND_DOMAIN}"
FRONTEND_CERTIFICATE_ARN = (
    "arn:aws:acm:us-east-1:851725545966:"
    "certificate/ee586878-a05c-463c-a4ac-f13ab9d17e9b"
)
API_CERTIFICATE_ARN = (
    "arn:aws:acm:eu-west-1:851725545966:"
    "certificate/97548b1e-afa4-42c3-8cf0-a0fa57e3edd9"
)
ROOMS_TABLE_NAME = "BluffOrBelieveRooms"

def find_template() -> Path:
    return Path(__file__).resolve().parent / "cdk.out" / "BluffOrBelieveStack.template.json"


def load_template(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        template = json.load(handle)
    if not isinstance(template, dict):
        raise TypeError("CloudFormation template root must be a JSON object.")
    return template


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def find_resources(template: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    return [
        resource
        for resource in _dict(template.get("Resources")).values()
        if isinstance(resource, dict) and resource.get("Type") == resource_type
    ]


def _getatt_target(value: Any) -> str | None:
    """Return the logical id of a {"Fn::GetAtt": [id, attr]} reference, if well formed."""
    attributes = _dict(value).get("Fn::GetAtt")
    if isinstance(attributes, list) and attributes and isinstance(attributes[0], str):
        return attributes[0]
    return None


def _ref_target(value: Any) -> str | None:
    target = _dict(value).get("Ref")
    return target if isinstance(target, str) else None


def check_task_role(
    template: dict[str, Any],
    task_definition: dict[str, Any],
    room_table_ids: set[str],
) -> list[str]:
    """The ECS task role may only GetItem/PutItem on the rooms table."""
    errors: list[str] = []
    resources = _dict(template.get("Resources"))
    role_id = _getatt_target(_dict(task_definition.get("Properties")).get("TaskRoleArn"))
    role = _dict(resources.get(role_id)) if role_id else {}
    if role.get("Type") != "AWS::IAM::Role":
        return ["ECS task definition must reference an IAM task role defined in the template"]

    # Policies attached to the task role: standalone policies plus inline ones.
    documents: list[Any] = []
    for policy in find_resources(template, "AWS::IAM::Policy"):
        properties = _dict(policy.get("Properties"))
        if role_id in {_ref_target(item) for item in _list(properties.get("Roles"))}:
            documents.append(properties.get("PolicyDocument"))
    role_properties = _dict(role.get("Properties"))
    for inline in _list(role_properties.get("Policies")):
        documents.append(_dict(inline).get("PolicyDocument"))
    if _list(role_properties.get("ManagedPolicyArns")):
        errors.append("task role must not attach managed policies")

    expected_actions = {"dynamodb:GetItem", "dynamodb:PutItem"}
    granted: set[str] = set()
    for document in documents:
        document = _dict(document)
        if not document:
            errors.append("task role policy document is missing or malformed")
            continue
        for statement in _list(document.get("Statement")):
            statement = _dict(statement)
            if statement.get("Effect") != "Allow":
                # Deny statements never expand access; anything else is malformed.
                if statement.get("Effect") != "Deny":
                    errors.append("task role policy statement has an invalid Effect")
                continue
            if "NotAction" in statement or "NotResource" in statement:
                errors.append("task role policy must not use NotAction or NotResource")
                continue
            actions = _list(statement.get("Action"))
            if not actions or not all(isinstance(action, str) for action in actions):
                errors.append("task role policy statement has missing or malformed Action")
                continue
            resources_ref = _list(statement.get("Resource"))
            targets_rooms_only = len(resources_ref) == 1 and (
                _getatt_target(resources_ref[0]) in room_table_ids
                and _dict(resources_ref[0])["Fn::GetAtt"][1:] == ["Arn"]
            )
            unexpected = set(actions) - expected_actions
            if unexpected:
                errors.append(
                    "task role policy grants unexpected actions: "
                    + ", ".join(sorted(unexpected))
                )
            if not targets_rooms_only:
                errors.append("task role policy must be scoped to the rooms table ARN only")
            if not unexpected and targets_rooms_only:
                granted |= set(actions)

    if granted != expected_actions:
        errors.append(
            "task IAM policy must grant only GetItem and PutItem on the rooms table"
        )
    return errors


def check_template(template: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    resources = template.get("Resources")
    if not isinstance(resources, dict) or not resources:
        return ["template must contain a non-empty Resources object"]

    # DynamoDB room table
    tables = [
        resource for resource in find_resources(template, "AWS::DynamoDB::Table")
        if _dict(resource.get("Properties")).get("TableName") == ROOMS_TABLE_NAME
    ]
    if len(tables) != 1:
        errors.append(f"expected exactly one DynamoDB table named {ROOMS_TABLE_NAME}")
    else:
        table = tables[0]
        props = _dict(table.get("Properties"))
        if props.get("BillingMode") != "PAY_PER_REQUEST":
            errors.append("rooms table must use PAY_PER_REQUEST billing")
        if props.get("TimeToLiveSpecification") != {
            "AttributeName": "expires_at", "Enabled": True
        }:
            errors.append("rooms table TTL must be enabled on expires_at")
        if props.get("SSESpecification", {}).get("SSEEnabled") is not True:
            errors.append("rooms table server-side encryption must be enabled")
        if props.get("PointInTimeRecoverySpecification", {}).get(
            "PointInTimeRecoveryEnabled"
        ) is not False:
            errors.append("rooms table point-in-time recovery must be explicitly disabled")
        if table.get("DeletionPolicy") != "Retain":
            errors.append("rooms table DeletionPolicy must be Retain")
        if table.get("UpdateReplacePolicy") != "Retain":
            errors.append("rooms table UpdateReplacePolicy must be Retain")

    # ECS task definition
    task_definitions = find_resources(template, "AWS::ECS::TaskDefinition")
    if len(task_definitions) != 1:
        errors.append("expected exactly one ECS task definition")
    else:
        task_props = _dict(task_definitions[0].get("Properties"))
        containers = _list(task_props.get("ContainerDefinitions"))
        if len(containers) != 1:
            errors.append("expected exactly one application container in task definition")
        else:
            container = _dict(containers[0])
            env = {
                item.get("Name"): item.get("Value")
                for item in map(_dict, _list(container.get("Environment")))
            }
            if env.get("APP_ENV") != "production":
                errors.append("ECS task APP_ENV must be production")
            if env.get("ROOMS_TABLE_NAME") != {
                "Ref": next(
                    (
                        logical_id for logical_id, resource in resources.items()
                        if resource.get("Type") == "AWS::DynamoDB::Table"
                        and _dict(resource.get("Properties")).get("TableName") == ROOMS_TABLE_NAME
                    ),
                    "__missing_rooms_table__",
                )
            }:
                errors.append("ECS task ROOMS_TABLE_NAME must reference the rooms table")
            cors = env.get("CORS_ORIGINS", "")
            if not isinstance(cors, str) or FRONTEND_ORIGIN not in cors.split(","):
                errors.append("ECS task CORS_ORIGINS must include the production frontend origin")
            if isinstance(cors, str) and any(
                "localhost" in origin.lower() or "127.0.0.1" in origin
                for origin in cors.split(",")
            ):
                errors.append("ECS task production CORS_ORIGINS must not include localhost")

            secrets = {
                item.get("Name"): item.get("ValueFrom")
                for item in map(_dict, _list(container.get("Secrets")))
            }
            secret_ref = secrets.get("ROOM_SESSION_SECRET")
            if not isinstance(secret_ref, dict) or not secret_ref.get("Fn::Join"):
                errors.append(
                    "ECS task must inject ROOM_SESSION_SECRET from Secrets Manager"
                )

    # Task role: only the required room-table operations
    room_table_ids = {
        logical_id for logical_id, resource in resources.items()
        if isinstance(resource, dict)
        and resource.get("Type") == "AWS::DynamoDB::Table"
        and _dict(resource.get("Properties")).get("TableName") == ROOMS_TABLE_NAME
    }
    if len(task_definitions) == 1:
        errors.extend(check_task_role(template, task_definitions[0], room_table_ids))

    # CloudFront distribution: custom alias and ACM certificate
    distributions = find_resources(template, "AWS::CloudFront::Distribution")
    matching_distribution = None
    for distribution in distributions:
        config = _dict(_dict(distribution.get("Properties")).get("DistributionConfig"))
        aliases = _list(config.get("Aliases"))
        if FRONTEND_DOMAIN in aliases:
            matching_distribution = config
            break
    if matching_distribution is None:
        errors.append(f"CloudFront distribution must include alias {FRONTEND_DOMAIN}")
    else:
        viewer_certificate = _dict(matching_distribution.get("ViewerCertificate"))
        if viewer_certificate.get("AcmCertificateArn") != FRONTEND_CERTIFICATE_ARN:
            errors.append("CloudFront distribution has the wrong ACM certificate ARN")
        if viewer_certificate.get("SslSupportMethod") != "sni-only":
            errors.append("CloudFront certificate must use SNI-only support")

    # ALB listener certificate
    listeners = find_resources(template, "AWS::ElasticLoadBalancingV2::Listener")
    api_certificate_found = any(
        _dict(certificate).get("CertificateArn") == API_CERTIFICATE_ARN
        for listener in listeners
        for certificate in _list(_dict(listener.get("Properties")).get("Certificates"))
    )
    if not api_certificate_found:
        errors.append("an ALB listener must use the expected API ACM certificate")

    return errors


def main() -> int:
    template_path = find_template()
    if not template_path.exists():
        print(f"FAIL: synthesized template not found: {template_path}")
        print("Run a locally configured CDK synthesis before validating.")
        return 1

    try:
        template = load_template(template_path)
        errors = check_template(template)
    except (OSError, json.JSONDecodeError, ValueError, TypeError, AttributeError) as exc:
        print(f"FAIL: unable to read CloudFormation template: {exc}")
        return 1

    if errors:
        print(f"FAIL: production template validation failed for {template_path}")
        for error in errors:
            print(f"  - {error}")
        return 1

    print(f"PASS: production template validation passed for {template_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
