from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

PRODUCTION_EXPECTATIONS = {
    "CDK_DEFAULT_ACCOUNT": "851725545966",
    "CDK_DEFAULT_REGION": "eu-west-1",
    "AWS_DEFAULT_REGION": "eu-west-1",
    "FRONTEND_DOMAIN_NAME": "play.ai-autofy-general-server.com",
    "FRONTEND_CERTIFICATE_ARN": (
        "arn:aws:acm:us-east-1:851725545966:certificate/ee586878-a05c-463c-a4ac-f13ab9d17e9b"
    ),
    "API_DOMAIN_NAME": "api.ai-autofy-general-server.com",
    "API_CERTIFICATE_ARN": (
        "arn:aws:acm:eu-west-1:851725545966:certificate/97548b1e-afa4-42c3-8cf0-a0fa57e3edd9"
    ),
    "FRONTEND_ORIGIN": "https://play.ai-autofy-general-server.com",
    "CORS_ORIGINS": "https://play.ai-autofy-general-server.com",
}


def validate_production_origins(
    frontend_domain_name: str,
    frontend_origin: str,
    cors_origins: str,
) -> None:
    allowed_origins = {origin.strip() for origin in cors_origins.split(",") if origin.strip()}
    if not frontend_domain_name or not frontend_origin or not allowed_origins:
        raise ValueError("Production frontend and CORS origins must be explicitly configured.")

    frontend_url = urlparse(frontend_origin)
    if (
        frontend_url.scheme != "https"
        or frontend_url.netloc != frontend_domain_name
        or frontend_origin not in allowed_origins
    ):
        raise ValueError(
            "FRONTEND_ORIGIN must be the HTTPS FRONTEND_DOMAIN_NAME and included in CORS_ORIGINS."
        )

    for origin in allowed_origins:
        parsed_origin = urlparse(origin)
        hostname = parsed_origin.hostname
        is_loopback = bool(
            hostname
            and (hostname.lower() == "localhost" or hostname.lower().endswith(".localhost"))
        )
        if hostname:
            try:
                is_loopback = is_loopback or ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                pass
        if is_loopback:
            raise ValueError("Production CORS_ORIGINS cannot contain localhost origins.")
        if (
            parsed_origin.scheme != "https"
            or hostname is None
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
            or parsed_origin.username
            or parsed_origin.password
        ):
            raise ValueError("Production CORS_ORIGINS must contain HTTPS origins only.")


def validate_production_environment(environ: dict[str, str]) -> None:
    app_env = environ.get("APP_ENV", "").lower()
    require_production = environ.get("REQUIRE_PRODUCTION_CONFIG", "false").lower() in {
        "1",
        "true",
        "yes",
        "y",
    }

    if app_env not in {"local", "production"}:
        raise ValueError("APP_ENV must be explicitly set to 'local' or 'production'.")

    if app_env != "production" and not require_production:
        return

    missing: list[str] = []
    mismatched: list[str] = []
    for key, expected in PRODUCTION_EXPECTATIONS.items():
        actual = environ.get(key)
        if actual is None:
            missing.append(key)
        elif actual != expected:
            mismatched.append(f"{key}: expected '{expected}' but got '{actual}'")

    if missing or mismatched:
        details = "; ".join(missing or mismatched)
        raise ValueError(
            "Production configuration guard failed. Set the expected deployment values "
            "before synth. "
            f"Missing or mismatched: {details}"
        )

    secret_arn = environ.get("ROOM_SESSION_SECRET_ARN", "")
    if not re.fullmatch(
        r"arn:aws:secretsmanager:eu-west-1:\d{12}:secret:[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}",
        secret_arn,
    ):
        raise ValueError(
            "ROOM_SESSION_SECRET_ARN must identify a Secrets Manager secret in eu-west-1."
        )

    validate_production_origins(
        environ["FRONTEND_DOMAIN_NAME"],
        environ["FRONTEND_ORIGIN"],
        environ["CORS_ORIGINS"],
    )
