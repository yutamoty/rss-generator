import hashlib
import json
import os
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from xml.etree.ElementTree import Element, SubElement, tostring

import logging
from urllib.error import HTTPError as URLHTTPError

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")

ssm = boto3.client("ssm")

table = dynamodb.Table(os.environ["SITES_TABLE"])
feed_bucket = os.environ["FEED_BUCKET"]

_jina_api_key = None


def get_jina_api_key():
    global _jina_api_key
    if _jina_api_key is None:
        response = ssm.get_parameter(
            Name=os.environ["JINA_API_KEY_PARAM"], WithDecryption=True
        )
        _jina_api_key = response["Parameter"]["Value"]
    return _jina_api_key

BEDROCK_MODEL_ID = "amazon.nova-lite-v2:0"

EXTRACTION_PROMPT = """\
以下のMarkdownはWebページの内容です。このページから記事・ニュース・更新情報の一覧を抽出してください。

以下のJSON配列形式で出力してください。JSON以外は出力しないでください。
[
  {
    "title": "記事タイトル",
    "link": "記事の絶対URL",
    "date": "公開日 (YYYY-MM-DD形式、不明なら空文字)",
    "summary": "記事の要約 (1-2文)"
  }
]

Markdown:
"""


def lambda_handler(event, context):
    site_id = event["site_id"]
    url = event["url"]
    name = event["name"]
    feed_path = event["feed_path"]
    last_hash = event.get("last_hash", "")

    markdown, page_title = fetch_markdown(url)

    content_hash = hashlib.sha256(markdown.encode()).hexdigest()
    if content_hash == last_hash:
        return {"site_id": site_id, "status": "skipped", "reason": "no_change"}

    articles = extract_articles(markdown)
    if not articles:
        return {"site_id": site_id, "status": "skipped", "reason": "no_articles"}

    display_name = name or page_title or url
    atom_xml = build_atom(display_name, url, articles)

    s3.put_object(
        Bucket=feed_bucket,
        Key=feed_path,
        Body=atom_xml,
        ContentType="application/atom+xml; charset=utf-8",
    )

    now = datetime.now(timezone.utc).isoformat()
    table.update_item(
        Key={"site_id": site_id},
        UpdateExpression="SET last_hash = :h, updated_at = :u",
        ExpressionAttributeValues={":h": content_hash, ":u": now},
    )

    return {"site_id": site_id, "status": "updated", "articles": len(articles)}


def fetch_markdown(url):
    jina_url = f"https://r.jina.ai/{url}"
    req = Request(
        jina_url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {get_jina_api_key()}",
        },
    )
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except URLHTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error("Jina API error: status=%s url=%s body=%s", e.code, jina_url, body)
        raise

    content = data.get("data", {}).get("content", "")
    title = data.get("data", {}).get("title", "")
    return content, title


def extract_articles(markdown):
    prompt = EXTRACTION_PROMPT + markdown

    body = json.dumps(
        {
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": 4096, "temperature": 0.0},
        }
    )

    response = bedrock.invoke_model(
        modelId=BEDROCK_MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=body,
    )
    result = json.loads(response["body"].read())

    text = result["output"]["message"]["content"][0]["text"]

    # Extract JSON array from response
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        return []

    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []


def build_atom(site_name, site_url, articles):
    ATOM_NS = "http://www.w3.org/2005/Atom"
    feed = Element("feed", xmlns=ATOM_NS)

    SubElement(feed, "title").text = site_name
    SubElement(feed, "link", href=site_url, rel="alternate")
    SubElement(feed, "updated").text = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    SubElement(feed, "id").text = site_url

    for article in articles:
        entry = SubElement(feed, "entry")
        SubElement(entry, "title").text = article.get("title", "")
        link = article.get("link", "")
        SubElement(entry, "link", href=link, rel="alternate")
        SubElement(entry, "id").text = link or article.get("title", "")

        date = article.get("date", "")
        if date:
            SubElement(entry, "updated").text = f"{date}T00:00:00Z"

        summary = article.get("summary", "")
        if summary:
            SubElement(entry, "summary").text = summary

    return b'<?xml version="1.0" encoding="utf-8"?>\n' + tostring(
        feed, encoding="unicode"
    ).encode("utf-8")
