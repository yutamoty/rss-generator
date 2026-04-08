import os

import boto3

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["SITES_TABLE"])


def lambda_handler(event, context):
    response = table.scan()
    items = response.get("Items", [])
    return {"sites": items}
