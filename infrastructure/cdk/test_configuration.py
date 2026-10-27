import pytest
from configuration import (
    PRODUCTION_EXPECTATIONS,
    validate_production_environment,
    validate_production_origins,
)


def test_production_configuration_requires_explicit_values() -> None:
    with pytest.raises(ValueError, match="Production configuration guard failed"):
        validate_production_environment({"APP_ENV": "production"})


def test_cdk_environment_must_be_explicitly_local_or_production() -> None:
    with pytest.raises(ValueError, match="APP_ENV must be explicitly set"):
        validate_production_environment({})
    with pytest.raises(ValueError, match="APP_ENV must be explicitly set"):
        validate_production_environment({"APP_ENV": "staging"})
    validate_production_environment({"APP_ENV": "local"})


@pytest.mark.parametrize(
    "development_origin",
    [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://[::1]",
        "https://dev.localhost",
    ],
)
def test_production_origins_reject_localhost(development_origin: str) -> None:
    with pytest.raises(ValueError, match="cannot contain localhost"):
        validate_production_origins(
            "play.example.com",
            "https://play.example.com",
            f"https://play.example.com,{development_origin}",
        )


def test_documented_production_origins_are_accepted() -> None:
    validate_production_origins(
        "play.ai-autofy-general-server.com",
        "https://play.ai-autofy-general-server.com",
        "https://play.ai-autofy-general-server.com",
    )

    environment = {
        **PRODUCTION_EXPECTATIONS,
        "APP_ENV": "production",
        "ROOM_SESSION_SECRET_ARN": (
            "arn:aws:secretsmanager:eu-west-1:123456789012:"
            "secret:BluffOrBelieveRoomSessionSecret-AbCd12"
        ),
    }
    validate_production_environment(environment)


def test_production_frontend_origin_must_be_in_cors_allowlist() -> None:
    with pytest.raises(ValueError, match="included in CORS_ORIGINS"):
        validate_production_origins(
            "play.example.com",
            "https://play.example.com",
            "https://other.example.com",
        )


def test_production_origins_reject_paths_and_trailing_slashes() -> None:
    with pytest.raises(ValueError, match="HTTPS origins only"):
        validate_production_origins(
            "play.example.com",
            "https://play.example.com/",
            "https://play.example.com/",
        )


def test_production_configuration_requires_region_scoped_session_secret() -> None:
    environment = {
        **PRODUCTION_EXPECTATIONS,
        "APP_ENV": "production",
    }
    with pytest.raises(ValueError, match="ROOM_SESSION_SECRET_ARN"):
        validate_production_environment(environment)

    environment["ROOM_SESSION_SECRET_ARN"] = (
        "arn:aws:secretsmanager:eu-west-1:123456789012:secret:incomplete"
    )
    with pytest.raises(ValueError, match="ROOM_SESSION_SECRET_ARN"):
        validate_production_environment(environment)

    environment["ROOM_SESSION_SECRET_ARN"] = (
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:wrong-region-AbCd12"
    )
    with pytest.raises(ValueError, match="ROOM_SESSION_SECRET_ARN"):
        validate_production_environment(environment)


@pytest.mark.parametrize(
    ("key", "from_region", "to_region"),
    [
        ("FRONTEND_CERTIFICATE_ARN", "us-east-1", "eu-west-1"),
        ("API_CERTIFICATE_ARN", "eu-west-1", "us-east-1"),
    ],
)
def test_production_configuration_rejects_certificate_region_mismatches(
    key: str,
    from_region: str,
    to_region: str,
) -> None:
    environment = {
        **PRODUCTION_EXPECTATIONS,
        "APP_ENV": "production",
        "ROOM_SESSION_SECRET_ARN": (
            "arn:aws:secretsmanager:eu-west-1:123456789012:"
            "secret:BluffOrBelieveRoomSessionSecret-AbCd12"
        ),
    }
    environment[key] = environment[key].replace(from_region, to_region, 1)

    with pytest.raises(ValueError, match="mismatched"):
        validate_production_environment(environment)
