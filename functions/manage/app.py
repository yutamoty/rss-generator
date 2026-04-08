import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import boto3
import ulid

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
table = dynamodb.Table(os.environ["SITES_TABLE"])
feed_bucket = os.environ["FEED_BUCKET"]
distribution_domain = os.environ.get("FEED_DISTRIBUTION_DOMAIN", "")


def lambda_handler(event, context):
    command = event.get("command")
    options = event.get("options", {})

    handlers = {
        "add": handle_add,
        "list": handle_list,
        "delete": handle_delete,
        "feeds": handle_feeds,
    }

    handler = handlers.get(command)
    if not handler:
        return {"content": f"Unknown command: {command}"}

    return handler(options)


def handle_add(options):
    url = options.get("url", "").strip()
    if not url:
        return {"content": "URL is required."}

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"content": "Invalid URL."}

    name = options.get("name", "").strip()
    if not name:
        name = parsed.netloc.removeprefix("www.")

    site_id = str(ulid.new())
    feed_path = f"feeds/{site_id}.xml"
    now = datetime.now(timezone.utc).isoformat()

    table.put_item(
        Item={
            "site_id": site_id,
            "url": url,
            "name": name,
            "feed_path": feed_path,
            "last_hash": "",
            "created_at": now,
            "updated_at": now,
        }
    )

    return {"content": f"Added: **{name}** (`{site_id}`)\n{url}"}


def handle_list(options):
    response = table.scan()
    items = response.get("Items", [])

    if not items:
        return {"content": "No sites registered."}

    lines = []
    for item in sorted(items, key=lambda x: x.get("created_at", "")):
        lines.append(f"- `{item['site_id']}` **{item['name']}**\n  {item['url']}")

    return {"content": "\n".join(lines)}


def handle_delete(options):
    site_id = options.get("site_id", "").strip()
    if not site_id:
        return {"content": "site_id is required."}

    response = table.get_item(Key={"site_id": site_id})
    item = response.get("Item")
    if not item:
        return {"content": f"Site not found: `{site_id}`"}

    feed_path = item.get("feed_path", "")
    if feed_path:
        try:
            s3.delete_object(Bucket=feed_bucket, Key=feed_path)
        except Exception:
            pass

    table.delete_item(Key={"site_id": site_id})

    return {"content": f"Deleted: **{item['name']}** (`{site_id}`)"}


def handle_feeds(options):
    response = table.scan()
    items = response.get("Items", [])

    if not items:
        return {"content": "No feeds available."}

    lines = []
    for item in sorted(items, key=lambda x: x.get("created_at", "")):
        feed_url = f"https://{distribution_domain}/{item['feed_path']}"
        lines.append(f"- **{item['name']}**\n  {feed_url}")

    return {"content": "\n".join(lines)}
