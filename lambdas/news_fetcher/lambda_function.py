"""
Lambda 1: News Fetcher
Runs 3x daily. Pulls AI governance news from NewsAPI and Arxiv.
Saves raw news items to DynamoDB, then triggers the post generator.
"""

import json
import os
import sys
import boto3
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

# Add project root to path for vendored SDK
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
try:
    from meerkat_sdk import MeerkatAgent
    _agent = MeerkatAgent(
        api_key=os.environ.get("MEERKAT_API_KEY", ""),
        agent_id=os.environ.get("MEERKAT_AGENT_ID", ""),
        name="X Posting Agent",
        domain="social",
        base_url=os.environ.get("MEERKAT_API_URL", "https://api.meerkatplatform.com"),
        auto_heartbeat=False,
    ) if os.environ.get("MEERKAT_API_KEY") and os.environ.get("MEERKAT_AGENT_ID") else None
except Exception:
    _agent = None

# AWS clients
dynamodb = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda")
secrets_client = boto3.client("secretsmanager")

# Table name (set via environment variable during deployment)
NEWS_TABLE = os.environ.get("NEWS_TABLE", "meerkat-news")
GENERATOR_FUNCTION = os.environ.get("GENERATOR_FUNCTION", "meerkat-post-generator")


def get_secret(secret_name):
    """Fetch a secret from AWS Secrets Manager."""
    response = secrets_client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


def fetch_newsapi(api_key):
    """Fetch AI governance news from NewsAPI."""
    # Search for AI governance, AI safety, prompt injection, AI agents news
    query = (
        '"AI governance" OR "AI safety" OR "prompt injection" OR '
        '"AI hallucination" OR "AI agents" OR "LLM security" OR '
        '"AI regulation" OR "agentic AI"'
    )
    params = urllib.parse.urlencode({
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 5,
        "apiKey": api_key,
    })
    url = f"https://newsapi.org/v2/everything?{params}"

    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as response:
        data = json.loads(response.read().decode())

    articles = []
    for article in data.get("articles", []):
        articles.append({
            "source": "newsapi",
            "title": article.get("title", ""),
            "description": article.get("description", ""),
            "url": article.get("url", ""),
            "published_at": article.get("publishedAt", ""),
        })
    return articles


def fetch_arxiv():
    """Fetch recent AI safety/governance papers from Arxiv."""
    query = urllib.parse.urlencode({
        "search_query": 'all:"AI safety" OR all:"prompt injection" OR all:"LLM guardrails" OR all:"AI governance"',
        "start": 0,
        "max_results": 5,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    })
    url = f"http://export.arxiv.org/api/query?{query}"

    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as response:
        # Arxiv returns XML — we'll do simple parsing
        xml_data = response.read().decode()

    # Simple XML extraction (no external library needed)
    papers = []
    entries = xml_data.split("<entry>")[1:]  # Skip the feed header
    for entry in entries:
        title = _extract_xml(entry, "title").replace("\n", " ").strip()
        summary = _extract_xml(entry, "summary").replace("\n", " ").strip()
        link = _extract_xml(entry, "id").strip()
        published = _extract_xml(entry, "published").strip()

        if title:
            papers.append({
                "source": "arxiv",
                "title": title,
                "description": summary[:500],  # Truncate long abstracts
                "url": link,
                "published_at": published,
            })
    return papers


def _extract_xml(text, tag):
    """Simple XML tag extraction without external libraries."""
    start = text.find(f"<{tag}>")
    end = text.find(f"</{tag}>")
    if start == -1 or end == -1:
        return ""
    return text[start + len(tag) + 2:end]


def save_news_items(items):
    """Save news items to DynamoDB and return new (unseen) items."""
    table = dynamodb.Table(NEWS_TABLE)
    new_items = []

    for item in items:
        # Use URL as unique key to avoid duplicates
        news_id = item["url"]
        try:
            # Only save if it doesn't already exist
            table.put_item(
                Item={
                    "news_id": news_id,
                    "source": item["source"],
                    "title": item["title"],
                    "description": item["description"],
                    "url": item["url"],
                    "published_at": item["published_at"],
                    "fetched_at": datetime.utcnow().isoformat(),
                    "post_generated": False,
                },
                ConditionExpression="attribute_not_exists(news_id)",
            )
            new_items.append(item)
        except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
            # Already exists — skip
            pass

    return new_items


def lambda_handler(event, context):
    """Main Lambda entry point."""
    print("Meerkat News Fetcher starting...")

    # Send heartbeat and check for delegations (runs 3x daily)
    if _agent:
        try:
            _agent.heartbeat(status="active", metadata={"lambda": "news_fetcher"})
        except Exception:
            pass
        try:
            delegations = _agent.poll_delegations()
            for task in delegations:
                _agent.log_action("delegation_received", task)
                print(f"Delegation received: {task}")
        except Exception:
            pass

    # Get API keys from Secrets Manager
    secrets = get_secret("meerkat-api-keys")
    newsapi_key = secrets.get("newsapi_key", "")

    all_articles = []

    # Fetch from NewsAPI
    try:
        newsapi_articles = fetch_newsapi(newsapi_key)
        all_articles.extend(newsapi_articles)
        print(f"Fetched {len(newsapi_articles)} articles from NewsAPI")
    except Exception as e:
        print(f"NewsAPI error: {e}")

    # Fetch from Arxiv
    try:
        arxiv_papers = fetch_arxiv()
        all_articles.extend(arxiv_papers)
        print(f"Fetched {len(arxiv_papers)} papers from Arxiv")
    except Exception as e:
        print(f"Arxiv error: {e}")

    # Save to DynamoDB and get only new items
    new_items = save_news_items(all_articles)
    print(f"Found {len(new_items)} new items (out of {len(all_articles)} total)")

    # If we have new items, trigger the post generator
    if new_items:
        # Send the top 3 most interesting items to the generator
        payload = {"news_items": new_items[:3]}
        lambda_client.invoke(
            FunctionName=GENERATOR_FUNCTION,
            InvocationType="Event",  # Async — don't wait for response
            Payload=json.dumps(payload),
        )
        print(f"Triggered post generator with {len(new_items[:3])} items")
    else:
        print("No new items found. Meerkat is napping.")

    # Report results to Meerkat
    if _agent:
        try:
            sources = list(set(a["source"] for a in all_articles))
            _agent.log_action("news_fetched", {
                "articles_found": len(all_articles),
                "new_articles": len(new_items),
                "sources": ", ".join(sources),
                "triggered_generator": len(new_items) > 0,
            })
        except Exception:
            pass

    return {
        "statusCode": 200,
        "body": json.dumps({
            "total_fetched": len(all_articles),
            "new_items": len(new_items),
        }),
    }
