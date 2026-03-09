"""
Lambda 3: Post Publisher
Called when you approve a draft. Posts the tweet to X/Twitter.
Uses OAuth 1.0a to authenticate with X API v2.
"""

import json
import os
import boto3
import hashlib
import hmac
import time
import urllib.request
import urllib.parse
import urllib.error
import uuid
from datetime import datetime

# AWS clients
dynamodb = boto3.resource("dynamodb")
secrets_client = boto3.client("secretsmanager")

# Environment variables
POSTS_TABLE = os.environ.get("POSTS_TABLE", "meerkat-posts")


def get_secret(secret_name):
    """Fetch secrets from AWS Secrets Manager."""
    response = secrets_client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


def create_oauth_signature(method, url, params, consumer_secret, token_secret):
    """Create OAuth 1.0a signature for X API."""
    # Sort parameters and encode
    sorted_params = sorted(params.items())
    param_string = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted_params
    )

    # Create signature base string
    base_string = (
        f"{method.upper()}&"
        f"{urllib.parse.quote(url, safe='')}&"
        f"{urllib.parse.quote(param_string, safe='')}"
    )

    # Create signing key
    signing_key = (
        f"{urllib.parse.quote(consumer_secret, safe='')}&"
        f"{urllib.parse.quote(token_secret, safe='')}"
    )

    # Generate HMAC-SHA1 signature
    import base64
    signature = base64.b64encode(
        hmac.new(
            signing_key.encode("utf-8"),
            base_string.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("utf-8")

    return signature


def post_to_x(text, secrets, reply_to=None):
    """Post a tweet using X API v2 with OAuth 1.0a."""
    url = "https://api.x.com/2/tweets"

    api_key = secrets["x_api_key"]
    api_secret = secrets["x_api_secret"]
    access_token = secrets["x_access_token"]
    access_token_secret = secrets["x_access_token_secret"]

    # OAuth parameters
    oauth_params = {
        "oauth_consumer_key": api_key,
        "oauth_nonce": uuid.uuid4().hex,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": access_token,
        "oauth_version": "1.0",
    }

    # Create signature
    signature = create_oauth_signature(
        "POST", url, oauth_params, api_secret, access_token_secret
    )
    oauth_params["oauth_signature"] = signature

    # Build Authorization header
    auth_header = "OAuth " + ", ".join(
        f'{urllib.parse.quote(k, safe="")}="{urllib.parse.quote(v, safe="")}"'
        for k, v in sorted(oauth_params.items())
    )

    # Make the request
    body = {"text": text}
    if reply_to:
        body["reply"] = {"in_reply_to_tweet_id": reply_to}
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": auth_header,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    # Retry once on 5xx errors — X API sometimes returns 503 but still posts
    last_error = None
    for attempt in range(2):
        try:
            # Must regenerate OAuth params per attempt (timestamp/nonce change)
            if attempt > 0:
                time.sleep(2)
                oauth_params = {
                    "oauth_consumer_key": api_key,
                    "oauth_nonce": uuid.uuid4().hex,
                    "oauth_signature_method": "HMAC-SHA1",
                    "oauth_timestamp": str(int(time.time())),
                    "oauth_token": access_token,
                    "oauth_version": "1.0",
                }
                signature = create_oauth_signature(
                    "POST", url, oauth_params, api_secret, access_token_secret
                )
                oauth_params["oauth_signature"] = signature
                auth_header = "OAuth " + ", ".join(
                    f'{urllib.parse.quote(k, safe="")}="{urllib.parse.quote(v, safe="")}"'
                    for k, v in sorted(oauth_params.items())
                )
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={
                        "Authorization": auth_header,
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )

            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode())
                return result
        except urllib.error.HTTPError as e:
            last_error = e
            status = e.code
            print(f"X API attempt {attempt + 1} failed: HTTP {status}")
            if status == 403:
                raise  # Auth error, don't retry
            if status == 409:
                # Duplicate tweet — X already has it, treat as success
                print("409 Conflict: tweet likely already posted. Treating as success.")
                return {"data": {"id": "duplicate-conflict"}}
            if status < 500 and status != 429:
                raise  # Client errors (except rate limit) won't improve on retry

    raise last_error


def update_post_status(post_id, status, tweet_id=None):
    """Update the post status in DynamoDB."""
    table = dynamodb.Table(POSTS_TABLE)

    update_expr = "SET #s = :status, published_at = :published_at"
    expr_values = {
        ":status": status,
        ":published_at": datetime.utcnow().isoformat(),
    }

    if tweet_id:
        update_expr += ", tweet_id = :tweet_id"
        expr_values[":tweet_id"] = tweet_id

    table.update_item(
        Key={"post_id": post_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=expr_values,
    )


def lambda_handler(event, context):
    """Main Lambda entry point — called via API Gateway when you approve."""
    print("Meerkat Post Publisher starting...")

    # Get post_id from the request
    if "queryStringParameters" in event:
        # Called via API Gateway
        params = event.get("queryStringParameters", {}) or {}
        post_id = params.get("id", "")
        action = params.get("action", "")
    else:
        # Called directly
        post_id = event.get("post_id", "")
        action = event.get("action", "approve")

    # Handle reply action — no post_id needed, just reply_to and text
    if action == "reply":
        if "queryStringParameters" in event:
            reply_to = params.get("reply_to", "")
            text = urllib.parse.unquote(params.get("text", ""))
        else:
            reply_to = event.get("reply_to", "")
            text = event.get("text", "")

        if not reply_to or not text:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "text/html"},
                "body": "<h1>Error: reply_to and text are required</h1>",
            }

        try:
            secrets = get_secret("meerkat-api-keys")
            result = post_to_x(text, secrets, reply_to=reply_to)
            tweet_id = result.get("data", {}).get("id", "unknown")
            print(f"Reply posted (tweet {tweet_id}) to {reply_to}: {text}")
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "text/html"},
                "body": (
                    f"<h1>Reply posted! The meerkat responds.</h1>"
                    f"<p>{text}</p>"
                    f"<p>Reply Tweet ID: {tweet_id}</p>"
                    f"<p>In reply to: {reply_to}</p>"
                ),
            }
        except Exception as e:
            print(f"Error posting reply to X: {e}")
            return {
                "statusCode": 500,
                "headers": {"Content-Type": "text/html"},
                "body": f"<h1>Error posting reply</h1><p>{str(e)}</p>",
            }

    if not post_id:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "text/html"},
            "body": "<h1>Error: No post ID provided</h1>",
        }

    # Get the draft from DynamoDB
    table = dynamodb.Table(POSTS_TABLE)
    response = table.get_item(Key={"post_id": post_id})
    post = response.get("Item")

    if not post:
        return {
            "statusCode": 404,
            "headers": {"Content-Type": "text/html"},
            "body": f"<h1>Post {post_id} not found</h1>",
        }

    if post["status"] != "DRAFT":
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "text/html"},
            "body": f"<h1>Post {post_id} is already {post['status']}</h1>",
        }

    if action == "reject":
        update_post_status(post_id, "REJECTED")
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/html"},
            "body": "<h1>Post rejected. The meerkat stands down.</h1>",
        }

    # Approve and publish
    try:
        secrets = get_secret("meerkat-api-keys")
        result = post_to_x(post["post_text"], secrets)
        tweet_id = result.get("data", {}).get("id", "unknown")
        update_post_status(post_id, "PUBLISHED", tweet_id)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/html"},
            "body": (
                f"<h1>Posted! The meerkat has spoken.</h1>"
                f"<p>{post['post_text']}</p>"
                f"<p>Tweet ID: {tweet_id}</p>"
            ),
        }
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            # 5xx from X — tweet may have actually posted despite the error.
            # Mark as UNCERTAIN so we don't re-post on retry.
            update_post_status(post_id, "UNCERTAIN")
            print(f"X API returned {e.code} — tweet may have posted. Check X.")
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "text/html"},
                "body": (
                    f"<h1>X returned an error ({e.code}), but the tweet may be live.</h1>"
                    f"<p>Check your X profile. The post has been marked UNCERTAIN to prevent duplicates.</p>"
                    f"<p>{post['post_text']}</p>"
                ),
            }
        update_post_status(post_id, "FAILED")
        print(f"Error posting to X: {e}")
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "text/html"},
            "body": f"<h1>Error posting to X</h1><p>{str(e)}</p>",
        }
    except Exception as e:
        update_post_status(post_id, "FAILED")
        print(f"Error posting to X: {e}")
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "text/html"},
            "body": f"<h1>Error posting to X</h1><p>{str(e)}</p>",
        }
