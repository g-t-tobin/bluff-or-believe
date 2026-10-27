#!/usr/bin/env python3

import os

from aws_cdk import App

from bluff_or_believe_stack import BluffOrBelieveStack
from configuration import validate_production_environment

validate_production_environment(os.environ)

app = App()
BluffOrBelieveStack(
    app,
    "BluffOrBelieveStack",
    env={"account": os.getenv("CDK_DEFAULT_ACCOUNT"), "region": "eu-west-1"},
)

app.synth()
