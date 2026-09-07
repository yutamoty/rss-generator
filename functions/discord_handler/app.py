import json
import os

import boto3
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

ssm = boto3.client("ssm")
lambda_client = boto3.client("lambda")

MANAGE_FUNCTION_NAME = os.environ["MANAGE_FUNCTION_NAME"]
DISCORD_PUBLIC_KEY_PARAM = os.environ["DISCORD_PUBLIC_KEY_PARAM"]

_public_key = None


def get_public_key():
    global _public_key
    if _public_key is None:
        response = ssm.get_parameter(
            Name=DISCORD_PUBLIC_KEY_PARAM, WithDecryption=True
        )
        _public_key = response["Parameter"]["Value"]
    return _public_key


def verify_signature(event):
    body = event.get("body", "")
    signature = event["headers"].get("x-signature-ed25519", "")
    timestamp = event["headers"].get("x-signature-timestamp", "")

    verify_key = VerifyKey(bytes.fromhex(get_public_key()))
    try:
        verify_key.verify(f"{timestamp}{body}".encode(), bytes.fromhex(signature))
        return True
    except (BadSignatureError, Exception):
        return False


def lambda_handler(event, context):
    if not verify_signature(event):
        return {"statusCode": 401, "body": "Invalid signature"}

    body = json.loads(event.get("body", "{}"))
    interaction_type = body.get("type")

    # PING
    if interaction_type == 1:
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"type": 1}),
        }

    # APPLICATION_COMMAND
    if interaction_type == 2:
        data = body.get("data", {})
        command_name = data.get("name", "")
        raw_options = data.get("options", [])
        options = {opt["name"]: opt["value"] for opt in raw_options}

        # Discord requires an ACK within 3 seconds. The actual work (DynamoDB,
        # S3, downstream Lambda invokes) is done asynchronously by `manage`,
        # which edits this deferred response via the interaction webhook once
        # it has a result.
        payload = {
            "command": command_name,
            "options": options,
            "application_id": body.get("application_id"),
            "token": body.get("token"),
        }
        lambda_client.invoke(
            FunctionName=MANAGE_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(payload),
        )

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"type": 5}),
        }

    return {
        "statusCode": 400,
        "body": "Unsupported interaction type",
    }
