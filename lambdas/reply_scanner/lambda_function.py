"""
Lambda 4: Reply Scanner
Runs every 30-60 minutes. Searches recent tweets from target accounts,
evaluates relevance via Claude, generates reply drafts, and sends them
for human approval before posting.

IMPORTANT: Requires X API Basic plan ($100/month) minimum for tweet
read access (GET /2/users/:id/tweets). Free tier only allows posting.
"""

import json
import os
import sys
import re
import boto3
import hashlib
import hmac
import time
import urllib.request
import urllib.parse
import uuid
import base64
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
bedrock = boto3.client("bedrock-runtime")
sns_client = boto3.client("sns")
secrets_client = boto3.client("secretsmanager")

# Environment variables
REPLIES_TABLE = os.environ.get("REPLIES_TABLE", "meerkat-replies")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-3-5-sonnet-20241022-v2:0")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
APPROVAL_URL = os.environ.get("APPROVAL_URL", "")

# Target accounts to monitor (configurable via env var, comma-separated)
DEFAULT_TARGETS = "AnthropicAI,OpenAI,GoogleDeepMind,MetaAI,MistralAI,ai_risks,AISafetyInst,FLI_org,GaryMarcus,DarioAmodei"
TARGET_ACCOUNTS = os.environ.get("TARGET_ACCOUNTS", DEFAULT_TARGETS).split(",")

# How far back to look for tweets (in minutes)
LOOKBACK_MINUTES = int(os.environ.get("LOOKBACK_MINUTES", "90"))

# Max replies to generate per invocation (to control Bedrock costs)
MAX_REPLIES_PER_RUN = int(os.environ.get("MAX_REPLIES_PER_RUN", "3"))

# Relevance evaluation system prompt
RELEVANCE_PROMPT = """You are evaluating whether a tweet is relevant to Meerkat Labs,
an AI governance company focused on AI safety, prompt injection defense, hallucination
detection, agent trust, and LLM output verification.

A tweet is RELEVANT if it discusses any of:
- AI safety, AI governance, AI regulation, or AI policy
- Prompt injection, jailbreaks, or adversarial attacks on LLMs
- AI hallucinations, factual accuracy of AI outputs, or AI reliability
- AI agents, autonomous AI systems, or multi-agent workflows
- LLM guardrails, output verification, or trust infrastructure
- Real-world AI failures or incidents

A tweet is NOT RELEVANT if it is:
- A generic product launch with no governance angle
- Purely about model benchmarks or performance metrics
- Hiring announcements, event promotions, or retweets
- About AI art, music, or entertainment with no safety angle

Respond with ONLY one word: RELEVANT or IRRELEVANT"""

# Reply generation system prompt
REPLY_PROMPT = """You are the social media voice of Meerkat Labs, an AI governance
company that builds trust infrastructure for AI agents.

You are writing a reply to another account's tweet. Rules:
- Be insightful and add value to the conversation. Offer a perspective they missed.
- Technical but accessible. Developers respect substance over hype.
- NEVER be promotional or salesy. Do not mention meerkatplatform.com in replies.
- NEVER use emojis.
- NEVER use em-dashes. Use periods, commas, or line breaks instead.
- Maximum 280 characters.
- Be conversational, not preachy. You are joining a discussion, not lecturing.
- Only include #MeerkatOnWatch if it fits naturally at the end. Do not force it.
- Do NOT start with "Great point" or similar sycophantic openers.

DO NOT: attack competitors, give medical/investment advice, use emojis, use em-dashes."""


def get_secret(secret_name):
    """Fetch secrets from AWS Secrets Manager."""
    response = secrets_client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


def create_oauth_signature(method, url, params, consumer_secret, token_secret):
    """Create OAuth 1.0a signature for X API."""
    sorted_params = sorted(params.items())
    param_string = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted_params
    )

    base_string = (
        f"{method.upper()}&"
        f"{urllib.parse.quote(url, safe='')}&"
        f"{urllib.parse.quote(param_string, safe='')}"
    )

    signing_key = (
        f"{urllib.parse.quote(consumer_secret, safe='')}&"
        f"{urllib.parse.quote(token_secret, safe='')}"
    )

    signature = base64.b64encode(
        hmac.new(
            signing_key.encode("utf-8"),
            base_string.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("utf-8")

    return signature


def x_api_get(url, query_params, secrets):
    """Make an authenticated GET request to the X API v2."""
    api_key = secrets["x_api_key"]
    api_secret = secrets["x_api_secret"]
    access_token = secrets["x_access_token"]
    access_token_secret = secrets["x_access_token_secret"]

    oauth_params = {
        "oauth_consumer_key": api_key,
        "oauth_nonce": uuid.uuid4().hex,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": access_token,
        "oauth_version": "1.0",
    }

    # Signature must include both OAuth params and query params
    all_params = {**oauth_params, **query_params}
    signature = create_oauth_signature(
        "GET", url, all_params, api_secret, access_token_secret
    )
    oauth_params["oauth_signature"] = signature

    auth_header = "OAuth " + ", ".join(
        f'{urllib.parse.quote(k, safe="")}="{urllib.parse.quote(v, safe="")}"'
        for k, v in sorted(oauth_params.items())
    )

    # Build full URL with query params
    if query_params:
        full_url = url + "?" + urllib.parse.urlencode(query_params)
    else:
        full_url = url

    req = urllib.request.Request(
        full_url,
        headers={"Authorization": auth_header},
        method="GET",
    )

    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode())


def get_user_id(username, secrets):
    """Resolve an X username to a user ID."""
    url = f"https://api.x.com/2/users/by/username/{username}"
    result = x_api_get(url, {}, secrets)
    return result.get("data", {}).get("id")


def get_recent_tweets(user_id, secrets):
    """Fetch recent tweets from a user. Requires Basic plan."""
    url = f"https://api.x.com/2/users/{user_id}/tweets"

    # Look back LOOKBACK_MINUTES from now
    start_time = (datetime.utcnow() - timedelta(minutes=LOOKBACK_MINUTES)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    query_params = {
        "max_results": "10",
        "start_time": start_time,
        "tweet.fields": "created_at,author_id,conversation_id",
        "exclude": "retweets,replies",
    }

    result = x_api_get(url, query_params, secrets)
    return result.get("data", []) or []


def is_already_seen(tweet_id):
    """Check if we've already processed this tweet."""
    table = dynamodb.Table(REPLIES_TABLE)
    response = table.get_item(Key={"tweet_id": tweet_id})
    return "Item" in response


def mark_as_seen(tweet_id, username, tweet_text, reply_text, status):
    """Record a tweet as seen in DynamoDB."""
    table = dynamodb.Table(REPLIES_TABLE)
    table.put_item(
        Item={
            "tweet_id": tweet_id,
            "username": username,
            "tweet_text": tweet_text,
            "reply_text": reply_text,
            "status": status,
            "created_at": datetime.utcnow().isoformat(),
        }
    )


def evaluate_relevance(tweet_text):
    """Ask Claude whether a tweet is relevant to AI governance."""
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 10,
        "system": RELEVANCE_PROMPT,
        "messages": [{"role": "user", "content": f"Tweet: {tweet_text}"}],
    })

    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=body.encode("utf-8"),
    )

    result = json.loads(response["body"].read())
    answer = result["content"][0]["text"].strip().upper()
    return "RELEVANT" in answer


def generate_reply(tweet_text, username):
    """Generate a reply in Meerkat's brand voice."""
    user_message = (
        f"@{username} posted this tweet:\n\n"
        f'"{tweet_text}"\n\n'
        f"Write a reply (under 280 characters). Be insightful, add a perspective "
        f"they missed. Do NOT use em-dashes. Only add #MeerkatOnWatch if it fits "
        f"naturally.\n\n"
        f"Respond with ONLY the reply text. No quotes, no explanation."
    )

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 300,
        "system": REPLY_PROMPT,
        "messages": [{"role": "user", "content": user_message}],
    })

    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=body.encode("utf-8"),
    )

    result = json.loads(response["body"].read())
    reply_text = result["content"][0]["text"].strip()

    # Strip em-dashes that slipped through
    reply_text = reply_text.replace("\u2014", ",")

    # Enforce 280 char limit
    if len(reply_text) > 280:
        shorten_body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 300,
            "system": REPLY_PROMPT,
            "messages": [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": reply_text},
                {"role": "user", "content": (
                    f"That's {len(reply_text)} characters. "
                    "Shorten to under 280. Keep the insight. No em-dashes. "
                    "Respond with ONLY the shortened reply."
                )},
            ],
        })
        response = bedrock.invoke_model(
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=shorten_body.encode("utf-8"),
        )
        result = json.loads(response["body"].read())
        reply_text = result["content"][0]["text"].strip()
        reply_text = reply_text.replace("\u2014", ",")

    # Hard truncate as last resort
    if len(reply_text) > 280:
        reply_text = reply_text[:277] + "..."

    return reply_text


def send_approval_sms(tweet_id, username, tweet_text, reply_text):
    """Send SMS with the draft reply for human approval."""
    # Build the approval URL that triggers the post_publisher's reply action
    approve_params = urllib.parse.urlencode({
        "action": "reply",
        "reply_to": tweet_id,
        "text": reply_text,
    })
    approve_link = f"{APPROVAL_URL}?{approve_params}"

    message = (
        f"MEERKAT REPLY DRAFT\n\n"
        f"Replying to @{username}:\n"
        f'"{tweet_text[:200]}"\n\n'
        f"Our reply ({len(reply_text)} chars):\n"
        f'"{reply_text}"\n\n'
        f"To approve:\n{approve_link}\n\n"
        f"To skip, ignore this message."
    )

    sns_client.publish(
        TopicArn=SNS_TOPIC_ARN,
        Message=message,
        Subject="Meerkat Reply Draft",
    )
    print(f"Approval SMS sent for reply to @{username} tweet {tweet_id}")


def lambda_handler(event, context):
    """Main Lambda entry point. Runs on schedule."""
    print("Meerkat Reply Scanner starting...")
    print(f"Monitoring accounts: {TARGET_ACCOUNTS}")
    print(f"Lookback window: {LOOKBACK_MINUTES} minutes")

    # Get X API credentials
    try:
        secrets = get_secret("meerkat-api-keys")
    except Exception as e:
        print(f"Error fetching secrets: {e}")
        return {"statusCode": 500, "body": f"Secrets error: {e}"}

    replies_generated = 0
    tweets_scanned = 0
    tweets_relevant = 0

    for username in TARGET_ACCOUNTS:
        username = username.strip()
        if not username:
            continue

        print(f"\n--- Scanning @{username} ---")

        # Resolve username to user ID
        try:
            user_id = get_user_id(username, secrets)
            if not user_id:
                print(f"Could not resolve @{username} to a user ID. Skipping.")
                continue
            print(f"@{username} -> user ID {user_id}")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"Rate limited on user lookup for @{username}. Stopping.")
                break
            print(f"Error looking up @{username}: {e}")
            continue

        # Fetch recent tweets
        try:
            tweets = get_recent_tweets(user_id, secrets)
            print(f"Found {len(tweets)} recent tweets from @{username}")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"Rate limited fetching tweets for @{username}. Stopping.")
                break
            if e.code == 403:
                print(
                    f"403 Forbidden fetching tweets for @{username}. "
                    "Your X API plan likely does not include read access. "
                    "Basic plan ($100/mo) is required. Stopping."
                )
                return {
                    "statusCode": 403,
                    "body": (
                        "X API read access denied. This feature requires the "
                        "Basic plan ($100/month) or higher. Check your X API "
                        "tier at developer.x.com."
                    ),
                }
            print(f"Error fetching tweets for @{username}: {e}")
            continue

        for tweet in tweets:
            tweet_id = tweet["id"]
            tweet_text = tweet["text"]
            tweets_scanned += 1

            # Skip if already seen
            if is_already_seen(tweet_id):
                continue

            # Small delay between Bedrock calls to avoid throttling
            time.sleep(1)

            # Evaluate relevance with Claude
            try:
                relevant = evaluate_relevance(tweet_text)
            except Exception as e:
                print(f"Error evaluating tweet {tweet_id}: {e}")
                mark_as_seen(tweet_id, username, tweet_text, "", "EVAL_ERROR")
                continue

            if not relevant:
                print(f"Tweet {tweet_id} not relevant. Skipping.")
                mark_as_seen(tweet_id, username, tweet_text, "", "IRRELEVANT")
                continue

            tweets_relevant += 1
            print(f"Tweet {tweet_id} is RELEVANT: {tweet_text[:80]}...")

            # Check if we've hit our per-run reply cap
            if replies_generated >= MAX_REPLIES_PER_RUN:
                print(f"Hit max replies per run ({MAX_REPLIES_PER_RUN}). Saving for next run.")
                mark_as_seen(tweet_id, username, tweet_text, "", "DEFERRED")
                continue

            # Generate a reply
            try:
                reply_text = generate_reply(tweet_text, username)
                print(f"Generated reply ({len(reply_text)} chars): {reply_text}")
            except Exception as e:
                print(f"Error generating reply for tweet {tweet_id}: {e}")
                mark_as_seen(tweet_id, username, tweet_text, "", "GEN_ERROR")
                continue

            # Record in DynamoDB
            mark_as_seen(tweet_id, username, tweet_text, reply_text, "PENDING_APPROVAL")

            # Send for human approval
            try:
                send_approval_sms(tweet_id, username, tweet_text, reply_text)
                replies_generated += 1
            except Exception as e:
                print(f"Error sending approval SMS: {e}")

    summary = {
        "tweets_scanned": tweets_scanned,
        "tweets_relevant": tweets_relevant,
        "replies_generated": replies_generated,
        "accounts_monitored": len(TARGET_ACCOUNTS),
    }
    print(f"\nReply Scanner complete: {json.dumps(summary)}")

    # Report results to Meerkat
    if _agent:
        try:
            _agent.log_action("replies_scanned", {
                "tweets_scanned": tweets_scanned,
                "tweets_relevant": tweets_relevant,
                "replies_drafted": replies_generated,
                "accounts_monitored": len(TARGET_ACCOUNTS),
            })
        except Exception:
            pass

    return {"statusCode": 200, "body": json.dumps(summary)}
