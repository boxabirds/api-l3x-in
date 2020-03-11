from io import BufferedIOBase
from time import time
from typing import (
    Mapping,
    Iterable,
    List,
    Optional,
    Union,
)
import json

from . import (
    HandledError,
    Log,
    Response,
)

from .helpers import  import_non_stdlib_module


def invoke_lambda(name: str, payload: dict, invoke_type: str = "RequestResponse") -> Response:
    """Trigger AWS Lambda execution."""
    if invoke_type not in ("DryRun", "RequestResponse", "Event"):
        raise HandledError(f"invalid invoke_type: {invoke_type}", status_code=400)

    response = Response()

    boto3 = import_non_stdlib_module("boto3")
    lambda_client = boto3.client("lambda")

    Log.debug("Invoking lambda %s", name)

    lambda_resp = lambda_client.invoke(
        FunctionName=name,
        InvocationType=invoke_type,
        Payload=json.dumps(payload))

    Log.debug("Lambda %s invocation succesful", name)

    if invoke_type in ("DryRun", "RequestResponse"):
        Log.debug("Deserializing Lambda response Payload")
        lambda_payload = json.load(lambda_resp["Payload"])
        response.put(lambda_payload)

    if not 200 <= lambda_resp["StatusCode"] < 300:
        raise HandledError(message=f"lambda response: {lambda_resp}",
                           status_code=lambda_resp["StatusCode"])

    return response


def publish_to_sns_topic(sns_topic: str, subject: str, content: dict) -> Response:
    """
    :returns: SNS MessageId
    """
    Log.info("Sending message with subject '%s' to SNS topic %s", subject, sns_topic)
    Log.debug("Message: %s", content)

    boto3 = import_non_stdlib_module("boto3")
    sns = boto3.client("sns")

    sns_response = sns.publish(
        TopicArn=sns_topic,
        Message=json.dumps(content),
        Subject=subject,
    )

    try:
        response = Response()
        response.put(sns_response["MessageId"])
        return response

    except KeyError:  # Hard exit to ensure Lambda is requeued for retrial
        raise SystemExit("Missing MessageId in SNS response")


def send_event_to_logstream(log_group: str, log_stream: str, message: Mapping) -> str:
    Log.debug("Send event content to CloudWatch LogGroup %s Stream %s",
              log_group, log_stream)

    boto3 = import_non_stdlib_module("boto3")
    boto_exceptions = import_non_stdlib_module("botocore.exceptions")
    client = boto3.client("logs")

    sequence_token = None
    done = False
    retrials = 3

    if message:
        Log.debug("message content: %s", message)
    else:
        raise HandledError("No content to send to Log Stream, aborting", status_code=500)

    event = {
        "timestamp": int(time() * 1000),  # milliseconds after Jan 1, 1970 00:00:00 UTC
        "message": json.dumps(message),
    }

    while (not done) and retrials > 0:
        try:
            if sequence_token:
                Log.debug("Found Stream sequence_token %s", sequence_token)
                client.put_log_events(
                    logGroupName=log_group,
                    logStreamName=log_stream,
                    logEvents=[event],
                    sequenceToken=sequence_token,
                )

            else:
                Log.debug("Trying put_log_events without Stream sequence_token")
                client.put_log_events(
                    logGroupName=log_group,
                    logStreamName=log_stream,
                    logEvents=[event],
                )

            done = True
            return "Successfully delivered event content " \
                   "to CloudWatch LogGroup %s Stream %s" % (log_group, log_stream)

        except boto_exceptions.ClientError as error:
            Log.warning("Catched CloudWatch Logs client error code %s",
                        error.response['Error']['Code'])

            if error.response['Error']['Code'] in ["DataAlreadyAcceptedException",
                                                   "InvalidSequenceTokenException"]:
                Log.debug(
                    "Fetching sequence_token from boto error response['Error']['Message'] %s",
                    error.response["Error"]["Message"])
                # NOTE: apparently there's no sequenceToken attribute in the response so we have
                # to parse response["Error"]["Message"] string
                sequence_token = error.response["Error"]["Message"].split(":")[-1].strip(" ")
                Log.debug("Setting sequence_token to %s", sequence_token)

                retrials -= 1

                if retrials > 0:
                    Log.warning("Retrying %d more time(s)...", retrials)

                else:
                    raise HandledError(
                        "Failed sending event content to CloudWatch Logs "
                        "after 3 retrials",
                        status_code=500)

            else:
                raise HandledError("Unexpected response from boto client: %s" % error,
                                   status_code=500)


def read_log_stream(log_group: str, log_stream: str) -> Iterable:
    """Return all events from log stream."""
    Log.debug("Read events from CloudWatch LogGroup %s Stream %s",
              log_group, log_stream)

    boto3 = import_non_stdlib_module("boto3")
    client = boto3.client("logs")

    resp = client.get_log_events(logGroupName=log_group,
                                 logStreamName=log_stream)

    return resp["events"]


def read_all_log_streams(log_group: str) -> Mapping:
    Log.info("Read all events from all CloudWatch LogGroup %s Streams", log_group)

    boto3 = import_non_stdlib_module("boto3")
    client = boto3.client("logs")
    resp = client.describe_log_streams(logGroupName=log_group)

    streams = [stream["logStreamName"] for stream in resp["logStreams"]]

    return {stream: read_log_stream(log_group=log_group, log_stream=stream)
            for stream in streams}


def put_object_to_s3_bucket(key: str, bucket: str,
                            body: Union[BufferedIOBase, bytes],
                            wait: Optional[bool] = False) -> Mapping:
    Log.info("Put key %s to S3 bucket %s", key, bucket)

    boto3 = import_non_stdlib_module("boto3")
    client = boto3.client("s3")

    response = client.put_object(Body=body, Bucket=bucket, Key=key)

    if wait:
        client.get_waiter("object_exists").wait(Bucket=bucket, Key=key)

    return response


def get_object_from_s3_bucket(key: str, bucket: str) -> BufferedIOBase:
    Log.info("Get key %s from S3 bucket %s", key, bucket)

    boto3 = import_non_stdlib_module("boto3")
    boto_exceptions = import_non_stdlib_module("botocore.exceptions")
    client = boto3.client("s3")

    try:
        response = client.response = client.get_object(Bucket=bucket, Key=key)
        return response["Body"]

    except boto_exceptions.ClientError as error:

        if error.response["Error"]["Code"] == "NoSuchKey":
            raise HandledError("Key %s not found in bucket %s" % (key, bucket), status_code=404)

        raise error


def trigger_ecs_fargate_task(task: str, cluster: str,
                             subnets: List[str], security_groups: List[str],
                             assign_public_ip: Optional[bool] = True,
                             overrides: Optional[Mapping] = None) -> Mapping:
    Log.info("Trigger Fargate task %s", task)

    boto3 = import_non_stdlib_module("boto3")
    client = boto3.client("ecs")

    if overrides:
        Log.info("Setting overrides to %s", overrides)
    else:
        overrides = {}

    response = client.run_task(
        cluster=cluster,
        taskDefinition=task,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": subnets,
                "securityGroups": security_groups,
                "assignPublicIp": "ENABLED" if assign_public_ip else "DISABLED",
            },
        },
        overrides=overrides,
    )

    Log.debug("Response: %s", response)
    return response
