#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

EXPECTED_VALUES = {
    "play.ai-autofy-general-server.com": "CloudFront alias",
    "arn:aws:acm:us-east-1:851725545966:certificate/ee586878-a05c-463c-a4ac-f13ab9d17e9b": "CloudFront certificate ARN",
    "arn:aws:acm:eu-west-1:851725545966:certificate/97548b1e-afa4-42c3-8cf0-a0fa57e3edd9": "ALB certificate ARN",
    "https://play.ai-autofy-general-server.com": "CORS origin",
    "/cdk-bootstrap/hnb659fds/version": "CDK bootstrap qualifier",
}


def find_template() -> Path:
    cdk_dir = Path(__file__).resolve().parent
    template = cdk_dir / "cdk.out" / "BluffOrBelieveStack.template.json"
    if not template.exists():
        raise FileNotFoundError(f"Synth template not found at {template}. Run 'cdk synth' first.")
    return template


def load_template(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def check_expected_values(template_text: str) -> list[str]:
    missing: list[str] = []
    for needle, label in EXPECTED_VALUES.items():
        if needle not in template_text:
            missing.append(f"{label} ({needle})")
    return missing


def main() -> int:
    template_path = find_template()
    template_text = load_template(template_path)
    missing = check_expected_values(template_text)

    if missing:
        print(f"FAIL: production template validation failed for {template_path}")
        for item in missing:
            print(f"  - missing: {item}")
        return 1

    print(f"PASS: production template validation passed for {template_path}")
    for needle, label in EXPECTED_VALUES.items():
        print(f"  - {label}: {needle}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
