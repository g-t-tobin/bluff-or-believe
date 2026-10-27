from __future__ import annotations

import os

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_ecs_patterns as ecs_patterns
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct


class BluffOrBelieveStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        frontend_domain_name = os.getenv("FRONTEND_DOMAIN_NAME")
        frontend_certificate_arn = os.getenv("FRONTEND_CERTIFICATE_ARN")
        api_domain_name = os.getenv("API_DOMAIN_NAME")
        api_certificate_arn = os.getenv("API_CERTIFICATE_ARN")
        room_session_secret_arn = os.getenv("ROOM_SESSION_SECRET_ARN")

        cors_origins = {
            origin.strip()
            for origin in os.getenv(
                "CORS_ORIGINS",
                "http://localhost:5173,http://127.0.0.1:5173",
            ).split(",")
            if origin.strip()
        }
        allow_destructive_destroy = os.getenv("ALLOW_DESTRUCTIVE_DESTROY", "false").lower() in {
            "1",
            "true",
            "yes",
            "y",
        }
        bucket_removal_policy = (
            RemovalPolicy.DESTROY if allow_destructive_destroy else RemovalPolicy.RETAIN
        )
        bucket_auto_delete_objects = allow_destructive_destroy
        log_removal_policy = (
            RemovalPolicy.DESTROY if allow_destructive_destroy else RemovalPolicy.RETAIN
        )

        frontend_certificate = (
            acm.Certificate.from_certificate_arn(
                self, "FrontendCertificate", frontend_certificate_arn
            )
            if frontend_certificate_arn and frontend_domain_name
            else None
        )

        frontend_bucket = s3.Bucket(
            self,
            "FrontendBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=bucket_removal_policy,
            auto_delete_objects=bucket_auto_delete_objects,
        )

        origin_identity = cloudfront.OriginAccessIdentity(self, "FrontendOriginAccessIdentity")
        frontend_bucket.grant_read(origin_identity)

        frontend_rewrite_function = cloudfront.Function(
            self,
            "FrontendSpaRewrite",
            code=cloudfront.FunctionCode.from_inline(
                """
function handler(event) {
    var request = event.request;
    if (request.uri.lastIndexOf(".") < request.uri.lastIndexOf("/")) {
        request.uri = "/index.html";
    }
    return request;
}
"""
            ),
        )

        distribution = cloudfront.Distribution(
            self,
            "FrontendDistribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3Origin(
                    frontend_bucket,
                    origin_access_identity=origin_identity,
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=frontend_rewrite_function,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    )
                ],
            ),
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            domain_names=[frontend_domain_name] if frontend_domain_name else None,
            certificate=frontend_certificate,
        )
        if frontend_domain_name:
            cors_origins.add(f"https://{distribution.distribution_domain_name}")

        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC)
            ],
        )

        cluster = ecs.Cluster(self, "BackendCluster", vpc=vpc)

        rooms_table = dynamodb.Table(
            self,
            "RoomsTable",
            table_name="BluffOrBelieveRooms",
            partition_key=dynamodb.Attribute(
                name="room_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            time_to_live_attribute="expires_at",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=False,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        api_certificate = (
            acm.Certificate.from_certificate_arn(self, "ApiCertificate", api_certificate_arn)
            if api_certificate_arn
            else None
        )
        room_session_secret = (
            secretsmanager.Secret.from_secret_complete_arn(
                self,
                "RoomSessionSecret",
                room_session_secret_arn,
            )
            if room_session_secret_arn
            else None
        )

        backend_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "BackendService",
            cluster=cluster,
            assign_public_ip=True,
            cpu=256,
            memory_limit_mib=512,
            desired_count=1,
            public_load_balancer=True,
            task_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            protocol=elbv2.ApplicationProtocol.HTTPS
            if api_certificate
            else elbv2.ApplicationProtocol.HTTP,
            certificate=api_certificate,
            redirect_http=bool(api_certificate),
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_asset("../../backend"),
                container_port=8000,
                enable_logging=True,
                log_driver=ecs.LogDrivers.aws_logs(
                    stream_prefix="bluff-or-believe-backend",
                    log_group=logs.LogGroup(
                        self,
                        "BackendLogGroup",
                        retention=logs.RetentionDays.TWO_WEEKS,
                        removal_policy=log_removal_policy,
                    ),
                ),
                environment={
                    "HOST": "0.0.0.0",
                    "PORT": "8000",
                    "AWS_REGION": self.region,
                    "APP_ENV": "production",
                    "CORS_ORIGINS": ",".join(sorted(cors_origins)),
                    "ROOMS_TABLE_NAME": rooms_table.table_name,
                },
                secrets=(
                    {
                        "ROOM_SESSION_SECRET": ecs.Secret.from_secrets_manager(
                            room_session_secret
                        )
                    }
                    if room_session_secret is not None
                    else {}
                ),
            ),
        )
        backend_service.task_definition.task_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem", "dynamodb:PutItem"],
                resources=[rooms_table.table_arn],
            )
        )

        backend_service.target_group.configure_health_check(
            path="/health",
            port="traffic-port",
            protocol=elbv2.Protocol.HTTP,
            healthy_http_codes="200-399",
            healthy_threshold_count=2,
            unhealthy_threshold_count=5,
            interval=Duration.seconds(30),
            timeout=Duration.seconds(5),
        )
        backend_service.service.connections.allow_from(
            backend_service.load_balancer.connections,
            ec2.Port.tcp(8000),
            "Allow inbound requests from the ALB to the backend task",
        )

        if api_domain_name:
            api_path_rewrite_function = cloudfront.Function(
                self,
                "ApiPathRewrite",
                code=cloudfront.FunctionCode.from_inline(
                    """
function handler(event) {
    var request = event.request;
    if (request.uri === "/api") {
        request.uri = "/";
    } else if (request.uri.indexOf("/api/") === 0) {
        request.uri = request.uri.substring(4);
    }
    return request;
}
"""
                ),
            )
            api_origin = origins.HttpOrigin(
                api_domain_name,
                protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
                read_timeout=Duration.seconds(60),
                keepalive_timeout=Duration.seconds(60),
            )
            distribution.add_behavior(
                "/api/*",
                api_origin,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=api_path_rewrite_function,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    )
                ],
            )

        CfnOutput(self, "FrontendDistributionDomainName", value=distribution.domain_name)
        CfnOutput(
            self,
            "BackendLoadBalancerDnsName",
            value=backend_service.load_balancer.load_balancer_dns_name,
        )
        CfnOutput(self, "FrontendBucketName", value=frontend_bucket.bucket_name)
        CfnOutput(self, "Region", value=self.region)
        CfnOutput(
            self,
            "DeploymentNote",
            value=(
                "Room state is persisted in the retained BluffOrBelieveRooms DynamoDB table; "
                "WebSocket connections are transient."
            ),
        )
