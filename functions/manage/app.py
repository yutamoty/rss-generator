import ipaddress
import json
import logging
import os
import re
import socket
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import boto3
from ulid import ULID

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_CONTENT_MAX_LENGTH = 2000

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
lambda_client = boto3.client("lambda")
sfn_client = boto3.client("stepfunctions")
table = dynamodb.Table(os.environ["SITES_TABLE"])
feed_bucket = os.environ["FEED_BUCKET"]
distribution_domain = os.environ.get("FEED_DISTRIBUTION_DOMAIN", "")
generate_feed_function = os.environ.get("GENERATE_FEED_FUNCTION_NAME", "")
state_machine_arn = os.environ.get("STATE_MACHINE_ARN", "")


def split_into_chunks(content, limit):
    """Split content into chunks at line breaks, each within `limit` chars.

    A single line longer than `limit` is hard-split as a last resort.
    """
    lines = content.split("\n")
    chunks = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(line) <= limit:
            current = line
        else:
            for i in range(0, len(line), limit):
                chunks.append(line[i : i + limit])

    if current:
        chunks.append(current)

    return chunks or [""]


def discord_request(method, url, payload):
    req = Request(
        url,
        data=json.dumps(payload).encode(),
        method=method,
        headers={
            "Content-Type": "application/json",
            # Discord's edge (Cloudflare) blocks requests carrying urllib's
            # default "Python-urllib/x.y" User-Agent as bot traffic (403),
            # independent of the interaction token's validity.
            "User-Agent": "DiscordBot (https://github.com/yutamoty/rss-generator, 1.0)",
        },
    )
    try:
        urlopen(req, timeout=10)
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error("Discord rejected %s %s: status=%s body=%s", method, url, e.code, body)
    except URLError:
        logger.exception("Failed to call Discord API: %s %s", method, url)


def send_followup(application_id, token, content):
    """Deliver the deferred Discord interaction result as one or more messages.

    application_id/token are absent when invoked directly (e.g. console
    testing), in which case there is no Discord interaction to update.

    Discord rejects message content over 2000 characters, and lists like
    /feeds and /list grow with the number of registered sites, so the
    content is split across multiple messages rather than truncated: the
    first chunk edits the deferred response, the rest are sent as
    additional followup messages.
    """
    if not application_id or not token:
        return

    webhook_base = f"{DISCORD_API_BASE}/webhooks/{application_id}/{token}"
    chunks = split_into_chunks(content, DISCORD_CONTENT_MAX_LENGTH)

    discord_request("PATCH", f"{webhook_base}/messages/@original", {"content": chunks[0]})
    for chunk in chunks[1:]:
        discord_request("POST", webhook_base, {"content": chunk})


def lambda_handler(event, context):
    command = event.get("command")
    options = event.get("options", {})
    application_id = event.get("application_id")
    token = event.get("token")

    handlers = {
        "add": handle_add,
        "list": handle_list,
        "delete": handle_delete,
        "feeds": handle_feeds,
        "generate": handle_generate,
    }

    handler = handlers.get(command)
    if not handler:
        result = {"content": f"Unknown command: {command}"}
    else:
        try:
            result = handler(options)
        except Exception:
            # This function is invoked asynchronously (Event) by discord-handler.
            # An unhandled exception would make Lambda auto-retry the invocation,
            # which could duplicate side effects (e.g. adding the same site
            # twice), so failures are contained and reported to Discord instead.
            logger.exception("Unhandled error in command handler: %s", command)
            result = {"content": "An error occurred while processing the command."}

    send_followup(application_id, token, result.get("content", "An error occurred."))
    return result


def is_public_hostname(hostname):
    """Reject loopback/private/link-local/reserved hosts to mitigate SSRF."""
    if not hostname:
        return False

    hostname = hostname.strip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
        return False

    try:
        ip = ipaddress.ip_address(hostname)
        addrs = [ip]
    except ValueError:
        try:
            infos = socket.getaddrinfo(hostname, None)
        except (socket.gaierror, UnicodeError):
            return False
        addrs = [ipaddress.ip_address(info[4][0]) for info in infos]

    for addr in addrs:
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return False

    return bool(addrs)


def handle_add(options):
    url = options.get("url", "").strip()
    if not url:
        return {"content": "URL is required."}

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"content": "Invalid URL."}

    if not is_public_hostname(parsed.hostname):
        return {"content": "Invalid URL: host is not a publicly reachable address."}

    name = options.get("name", "").strip()
    if not name:
        name = parsed.netloc.removeprefix("www.")

    site_id = str(ULID())
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

    if generate_feed_function:
        lambda_client.invoke(
            FunctionName=generate_feed_function,
            InvocationType="Event",
            Payload=json.dumps({
                "site_id": site_id,
                "url": url,
                "name": name,
                "feed_path": feed_path,
                "last_hash": "",
            }),
        )

    return {"content": f"Added: **{name}** (`{site_id}`)\n{url}\nFeed generation started."}


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


def handle_generate(options):
    site_id = options.get("site_id", "").strip()

    if site_id:
        response = table.get_item(Key={"site_id": site_id})
        item = response.get("Item")
        if not item:
            return {"content": f"Site not found: `{site_id}`"}

        lambda_client.invoke(
            FunctionName=generate_feed_function,
            InvocationType="Event",
            Payload=json.dumps({
                "site_id": item["site_id"],
                "url": item["url"],
                "name": item["name"],
                "feed_path": item["feed_path"],
                "last_hash": item.get("last_hash", ""),
            }),
        )
        return {"content": f"Feed generation started: **{item['name']}**"}

    sfn_client.start_execution(stateMachineArn=state_machine_arn)
    return {"content": "Feed generation started for all sites."}


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
