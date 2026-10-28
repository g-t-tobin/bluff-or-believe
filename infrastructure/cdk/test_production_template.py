import json

import pytest

from validate_production_template import check_template, load_template


def valid_template() -> dict:
    return {
        "Resources": {
            "RoomsTable": {
                "Type": "AWS::DynamoDB::Table",
                "DeletionPolicy": "Retain",
                "UpdateReplacePolicy": "Retain",
                "Properties": {
                    "TableName": "BluffOrBelieveRooms",
                    "BillingMode": "PAY_PER_REQUEST",
                    "TimeToLiveSpecification": {
                        "AttributeName": "expires_at",
                        "Enabled": True,
                    },
                    "SSESpecification": {"SSEEnabled": True},
                    "PointInTimeRecoverySpecification": {
                        "PointInTimeRecoveryEnabled": False,
                    },
                },
            },
            "TaskDefinition": {
                "Type": "AWS::ECS::TaskDefinition",
                "Properties": {
                    "TaskRoleArn": {"Fn::GetAtt": ["TaskRole", "Arn"]},
                    "ContainerDefinitions": [{
                        "Environment": [
                            {"Name": "APP_ENV", "Value": "production"},
                            {
                                "Name": "ROOMS_TABLE_NAME",
                                "Value": {"Ref": "RoomsTable"},
                            },
                            {
                                "Name": "CORS_ORIGINS",
                                "Value": "https://play.ai-autofy-general-server.com",
                            },
                        ],
                        "Secrets": [{
                            "Name": "ROOM_SESSION_SECRET",
                            "ValueFrom": (
                                "arn:aws:secretsmanager:eu-west-1:"
                                "851725545966:secret:TestRoomSessionSecret-AbCd12"
                            ),
                        }],
                    }],
                },
            },
            "TaskRole": {"Type": "AWS::IAM::Role", "Properties": {}},
            "ExecutionRole": {"Type": "AWS::IAM::Role", "Properties": {}},
            "TaskPolicy": {
                "Type": "AWS::IAM::Policy",
                "Properties": {
                    "Roles": [{"Ref": "TaskRole"}],
                    "PolicyDocument": {
                        "Statement": [{
                            "Action": ["dynamodb:GetItem", "dynamodb:PutItem"],
                            "Effect": "Allow",
                            "Resource": {"Fn::GetAtt": ["RoomsTable", "Arn"]},
                        }],
                    },
                },
            },
            "FrontendDistribution": {
                "Type": "AWS::CloudFront::Distribution",
                "Properties": {
                    "DistributionConfig": {
                        "Aliases": ["play.ai-autofy-general-server.com"],
                        "ViewerCertificate": {
                            "AcmCertificateArn": (
                                "arn:aws:acm:us-east-1:851725545966:"
                                "certificate/ee586878-a05c-463c-a4ac-f13ab9d17e9b"
                            ),
                            "SslSupportMethod": "sni-only",
                        },
                    },
                },
            },
            "ApiListener": {
                "Type": "AWS::ElasticLoadBalancingV2::Listener",
                "Properties": {
                    "Certificates": [{
                        "CertificateArn": (
                            "arn:aws:acm:eu-west-1:851725545966:"
                            "certificate/97548b1e-afa4-42c3-8cf0-a0fa57e3edd9"
                        ),
                    }],
                },
            },
        },
    }


def test_valid_template_passes_structural_checks() -> None:
    assert check_template(valid_template()) == []


def test_missing_room_session_secret_is_reported() -> None:
    template = valid_template()
    del template["Resources"]["TaskDefinition"]["Properties"][
        "ContainerDefinitions"
    ][0]["Secrets"]

    errors = check_template(template)

    assert any("ROOM_SESSION_SECRET" in error for error in errors)


def test_localhost_cors_is_rejected() -> None:
    template = valid_template()
    container = template["Resources"]["TaskDefinition"]["Properties"][
        "ContainerDefinitions"
    ][0]
    for env in container["Environment"]:
        if env["Name"] == "CORS_ORIGINS":
            env["Value"] += ",http://localhost:5173"

    errors = check_template(template)

    assert any("must not include localhost" in error for error in errors)


def test_table_must_be_retained_and_have_ttl() -> None:
    template = valid_template()
    table = template["Resources"]["RoomsTable"]
    table["DeletionPolicy"] = "Delete"
    table["Properties"]["TimeToLiveSpecification"]["Enabled"] = False

    errors = check_template(template)

    assert any("DeletionPolicy must be Retain" in error for error in errors)
    assert any("TTL must be enabled" in error for error in errors)


def test_template_loader_reads_json(tmp_path) -> None:
    expected = valid_template()
    path = tmp_path / "template.json"
    path.write_text(json.dumps(expected), encoding="utf-8")

    assert load_template(path) == expected


def task_statements(template: dict) -> list:
    return template["Resources"]["TaskPolicy"]["Properties"]["PolicyDocument"]["Statement"]


def errors_for(template: dict) -> list[str]:
    return check_template(template)


def test_wildcard_action_on_task_role_is_rejected() -> None:
    template = valid_template()
    task_statements(template)[0]["Action"] = "*"

    assert any("unexpected actions" in e for e in errors_for(template))


def test_dynamodb_wildcard_and_extra_actions_are_rejected() -> None:
    template = valid_template()
    task_statements(template)[0]["Action"].append("dynamodb:*")

    assert any("dynamodb:*" in e for e in errors_for(template))


def test_wildcard_resource_on_task_role_is_rejected() -> None:
    template = valid_template()
    task_statements(template)[0]["Resource"] = "*"

    assert any("scoped to the rooms table" in e for e in errors_for(template))


def test_extra_statement_on_task_role_is_rejected() -> None:
    template = valid_template()
    task_statements(template).append(
        {"Action": "s3:GetObject", "Effect": "Allow", "Resource": "*"}
    )

    assert any("s3:GetObject" in e for e in errors_for(template))


def test_missing_put_item_is_rejected() -> None:
    template = valid_template()
    task_statements(template)[0]["Action"] = ["dynamodb:GetItem"]

    assert any("must grant only GetItem and PutItem" in e for e in errors_for(template))


def test_policy_on_another_role_is_not_mistaken_for_task_role() -> None:
    template = valid_template()
    template["Resources"]["TaskPolicy"]["Properties"]["Roles"] = [{"Ref": "ExecutionRole"}]

    assert any("must grant only GetItem and PutItem" in e for e in errors_for(template))


def test_unrelated_policies_on_other_roles_are_allowed() -> None:
    template = valid_template()
    template["Resources"]["ExecutionPolicy"] = {
        "Type": "AWS::IAM::Policy",
        "Properties": {
            "Roles": [{"Ref": "ExecutionRole"}],
            "PolicyDocument": {"Statement": [
                {"Action": "ecr:GetAuthorizationToken", "Effect": "Allow", "Resource": "*"},
                {"Action": ["logs:PutLogEvents"], "Effect": "Allow", "Resource": "*"},
            ]},
        },
    }

    assert errors_for(template) == []


def test_task_role_inline_and_managed_policies_are_checked() -> None:
    template = valid_template()
    template["Resources"]["TaskRole"]["Properties"] = {
        "ManagedPolicyArns": ["arn:aws:iam::aws:policy/AdministratorAccess"],
        "Policies": [{"PolicyDocument": {"Statement": [
            {"Action": "sqs:*", "Effect": "Allow", "Resource": "*"}
        ]}}],
    }

    errors = errors_for(template)
    assert any("managed policies" in e for e in errors)
    assert any("sqs:*" in e for e in errors)


def test_task_definition_must_reference_a_defined_task_role() -> None:
    template = valid_template()
    del template["Resources"]["TaskDefinition"]["Properties"]["TaskRoleArn"]

    assert any("IAM task role" in e for e in errors_for(template))


def test_not_action_statement_is_rejected() -> None:
    template = valid_template()
    task_statements(template).append(
        {"NotAction": "iam:*", "Effect": "Allow", "Resource": "*"}
    )

    assert any("NotAction" in e for e in errors_for(template))


@pytest.mark.parametrize("resources", [None, [], "x", {}])
def test_missing_or_malformed_resources_fail_closed(resources) -> None:
    assert check_template({"Resources": resources})
    assert check_template({})


def test_malformed_properties_are_reported_not_raised() -> None:
    template = valid_template()
    template["Resources"]["RoomsTable"]["Properties"] = "bad"
    template["Resources"]["TaskDefinition"]["Properties"] = []
    template["Resources"]["ApiListener"]["Properties"]["Certificates"] = ["bad", None]
    template["Resources"]["FrontendDistribution"]["Properties"] = None
    template["Resources"]["TaskPolicy"]["Properties"]["PolicyDocument"] = "bad"

    assert check_template(template)
