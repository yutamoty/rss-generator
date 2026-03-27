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

        payload = {"command": command_name, "options": options}
        response = lambda_client.invoke(
            FunctionName=MANAGE_FUNCTION_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )
        result = json.loads(response["Payload"].read())
        content = result.get("content", "An error occurred.")

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"type": 4, "data": {"content": content}}),
        }

    return {
        "statusCode": 400,
        "body": "Unsupported interaction type",
    }
