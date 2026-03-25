"""
Lambda 2: Post Generator
Receives news items from the fetcher, sends them to Claude via Bedrock,
saves draft posts to DynamoDB (X + LinkedIn versions), and sends SMS for approval.
"""

import json
import os
import re
import sys
import boto3
import uuid
from datetime import datetime

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

# Environment variables (set during deployment)
POSTS_TABLE = os.environ.get("POSTS_TABLE", "meerkat-posts")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-3-5-sonnet-20241022-v2:0")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
APPROVAL_URL = os.environ.get("APPROVAL_URL", "")

# Brand voice system prompt (embedded directly so Lambda stays self-contained)
SYSTEM_PROMPT = """You are the social media voice of Meerkat Labs, an AI governance
company that builds trust infrastructure for AI agents.

What Meerkat Labs does:
- INGRESS SHIELD: Scans inputs before the LLM processes them. Catches prompt
  injection, jailbreaks, data exfiltration across 8 attack categories.
- EGRESS VERIFY: Up to five ML checks before any action executes.
- REMEDIATION: When errors are caught, the agent self-corrects automatically.

Product URL: meerkatplatform.com

Voice rules:
- Technical but accessible. Developers respect substance over hype.
- Confident, not salesy.
- Use concrete examples over abstract claims.
- Short, punchy. Think engineering tweets, not marketing copy.
- NEVER use emojis.
- NEVER use em-dashes (—). Use periods, commas, or line breaks instead.
- Maximum 280 characters per post. URLs count as 23 characters regardless of length.
- Only include meerkatplatform.com in about 30% of posts.
- When referencing a news article, research paper, or external source, ALWAYS include the source URL at the end of the post.
- No hashtags unless highly relevant.

DO NOT: attack competitors, give medical/investment advice, use emojis, use em-dashes (—)."""

# LinkedIn feature flag and prompt
LINKEDIN_ENABLED = os.environ.get("LINKEDIN_ENABLED", "false").lower() == "true"

LINKEDIN_SYSTEM_PROMPT = """You are writing a LinkedIn post for Jean Raubenheimer, MD,
founder of Meerkat Labs Inc. Jean is a practicing anesthesiologist at Providence Health
Care in Vancouver, building AI verification infrastructure for regulated industries.

Tone: Professional thought leadership. Not corporate jargon. Authentic, direct,
clinician-turned-founder perspective. Occasional personal insight from the OR or
the startup trenches.

Format:
- Opening hook (1-2 lines that make people stop scrolling)
- 3-5 short paragraphs of substance
- 1-3 relevant hashtags at the end (max)
- NEVER use emojis
- NEVER use em-dashes. Use periods, commas, or line breaks instead.
- 600-1200 characters total

What Meerkat Labs does:
- Real-time AI governance middleware for regulated industries
- Dual-gate: ingress shield (prompt injection defense) + egress verification (hallucination detection)
- Sub-2-second latency, full audit trail, purpose-built verification models
- Product URL: meerkatplatform.com

DO NOT: attack competitors, give medical/investment advice, use emojis, use em-dashes (—).
DO NOT: use "excited to announce" or "thrilled to share" or any LinkedIn cliches."""


def _twitter_length(text):
    """Calculate tweet length accounting for t.co URL shortening (23 chars per URL)."""
    urls = re.findall(r'https?://\S+', text)
    length = len(text)
    for url in urls:
        length = length - len(url) + 23
    return length


def generate_post(news_items):
    """Send news to Claude and get a draft post back."""
    # Build the news summary for Claude
    news_text = ""
    for i, item in enumerate(news_items, 1):
        news_text += f"\n{i}. [{item['source'].upper()}] {item['title']}\n"
        news_text += f"   {item['description'][:300]}\n"
        news_text += f"   URL: {item['url']}\n"

    user_message = f"""Here are today's top AI governance news items:
{news_text}

Pick the MOST interesting item and write a single X/Twitter post about it.
The post MUST be under 280 characters (URLs count as 23 characters regardless of actual length).
Be punchy and insightful. Connect it to why AI governance or verification matters.
You MUST include the source URL at the end of the post for attribution.
Do NOT use em-dashes (—) anywhere.

Respond with ONLY the post text, nothing else. No quotes, no explanation."""

    # Call Claude via Bedrock
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 300,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_message}],
    })

    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=body.encode("utf-8"),
    )

    result = json.loads(response["body"].read())
    post_text = result["content"][0]["text"].strip()

    # Safety check: enforce 280 char limit (Twitter counts URLs as 23 chars)
    twitter_length = _twitter_length(post_text)
    if twitter_length > 280:
        # Ask Claude to shorten it
        shorten_body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 300,
            "system": SYSTEM_PROMPT,
            "messages": [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": post_text},
                {"role": "user", "content": f"That's {len(post_text)} characters. "
                 "Shorten it to under 280 characters (URLs count as 23 chars). "
                 "Keep the punch. Keep the source URL. Do NOT use em-dashes. "
                 "Respond with ONLY the shortened post."},
            ],
        })
        response = bedrock.invoke_model(
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=shorten_body.encode("utf-8"),
        )
        result = json.loads(response["body"].read())
        post_text = result["content"][0]["text"].strip()

    # Strip any em-dashes that slipped through
    post_text = post_text.replace("—", ",")

    # Ensure source attribution: if no URL is present, append the first news item's URL
    has_url = "http://" in post_text or "https://" in post_text
    if not has_url and news_items:
        source_url = news_items[0]["url"]
        # Twitter counts URLs as 23 chars; check if we have room
        # (post text + space + URL-as-23-chars must fit in 280)
        twitter_length = len(post_text) + 1 + 23
        if twitter_length <= 280:
            post_text = f"{post_text} {source_url}"
        else:
            # Trim post text to make room for the URL
            max_text_len = 280 - 1 - 23  # 256 chars for text
            post_text = post_text[:max_text_len].rstrip() + " " + source_url

    return post_text


def generate_linkedin_post(news_items):
    """Generate a LinkedIn post from the same news items."""
    news_text = ""
    for i, item in enumerate(news_items, 1):
        news_text += f"\n{i}. [{item['source'].upper()}] {item['title']}\n"
        news_text += f"   {item['description'][:300]}\n"
        news_text += f"   URL: {item['url']}\n"

    user_message = f"""Here are today's top AI governance news items:
{news_text}

Pick the MOST interesting item and write a LinkedIn post about it.
Write as Jean Raubenheimer, a physician-founder building AI safety tools.
Connect the news to real clinical or enterprise risks.
Include the source URL for attribution.
Do NOT use em-dashes anywhere.

Respond with ONLY the post text, nothing else."""

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1000,
        "system": LINKEDIN_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_message}],
    })

    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=body.encode("utf-8"),
    )

    result = json.loads(response["body"].read())
    post_text = result["content"][0]["text"].strip()

    # Strip em-dashes
    post_text = post_text.replace("\u2014", ",")

    return post_text


def save_draft(post_text, news_items, platform="x"):
    """Save the draft post to DynamoDB."""
    table = dynamodb.Table(POSTS_TABLE)
    post_id = str(uuid.uuid4())[:8]

    item = {
        "post_id": post_id,
        "post_text": post_text,
        "platform": platform,
        "status": "DRAFT",
        "char_count": len(post_text),
        "source_news": json.dumps([{"title": it["title"], "url": it["url"]} for it in news_items]),
        "created_at": datetime.utcnow().isoformat(),
        "published_at": None,
    }
    table.put_item(Item=item)
    return post_id


def send_approval_sms(post_id, post_text):
    """Send SMS with draft post for approval."""
    message = (
        f"MEERKAT DRAFT [{post_id}]\n\n"
        f"{post_text}\n\n"
        f"({len(post_text)} chars)\n\n"
        f"To approve, visit:\n"
        f"{APPROVAL_URL}?action=approve&id={post_id}\n\n"
        f"To reject, ignore this message."
    )

    sns_client.publish(
        TopicArn=SNS_TOPIC_ARN,
        Message=message,
        Subject="Meerkat Draft Post",
    )
    print(f"SMS sent for post {post_id}")


def lambda_handler(event, context):
    """Main Lambda entry point."""
    print("Meerkat Post Generator starting...")

    news_items = event.get("news_items", [])
    if not news_items:
        print("No news items received. Nothing to generate.")
        return {"statusCode": 200, "body": "No news items"}

    print(f"Generating post from {len(news_items)} news items...")

    # Generate the post using Claude
    try:
        post_text = generate_post(news_items)
        print(f"Generated post ({len(post_text)} chars): {post_text}")
    except Exception as e:
        print(f"Error generating post: {e}")
        return {"statusCode": 500, "body": f"Generation error: {e}"}

    # Build source context for verification
    source_context = "\n".join(
        f"[{item['source'].upper()}] {item['title']}: {item['description'][:300]}"
        for item in news_items
    )

    # --- Verify and save X post ---
    x_trust = None
    x_passed = True
    if _agent:
        try:
            result = _agent.verify(output=post_text, context=source_context)
            x_trust = result.trust_score
            x_passed = result.passed
            print(f"X verify: trust={x_trust}, passed={x_passed}")
            if not x_passed:
                _agent.alert(f"X post held: trust score {x_trust}", severity="warning")
        except Exception as e:
            print(f"X verify error (non-blocking): {e}")

    x_post_id = save_draft(post_text, news_items, platform="x")
    print(f"X draft saved: {x_post_id}")

    if x_passed:
        try:
            send_approval_sms(x_post_id, post_text)
        except Exception as e:
            print(f"Error sending SMS for X: {e}")

    if _agent:
        try:
            _agent.log_action("x_post_drafted", {
                "post_id": x_post_id,
                "platform": "x",
                "char_count": len(post_text),
                "content_preview": post_text[:80],
                "status": "HELD" if not x_passed else "DRAFT",
                "trust_score": x_trust,
            })
        except Exception:
            pass

    # --- Generate, verify, and save LinkedIn post ---
    li_post_id = None
    li_trust = None
    li_text = None
    if LINKEDIN_ENABLED:
        try:
            li_text = generate_linkedin_post(news_items)
            print(f"LinkedIn post ({len(li_text)} chars): {li_text[:100]}...")

            li_passed = True
            if _agent:
                try:
                    result = _agent.verify(output=li_text, context=source_context)
                    li_trust = result.trust_score
                    li_passed = result.passed
                    print(f"LinkedIn verify: trust={li_trust}, passed={li_passed}")
                    if not li_passed:
                        _agent.alert(f"LinkedIn post held: trust score {li_trust}", severity="warning")
                except Exception as e:
                    print(f"LinkedIn verify error (non-blocking): {e}")

            li_post_id = save_draft(li_text, news_items, platform="linkedin")
            print(f"LinkedIn draft saved: {li_post_id}")

            if li_passed:
                try:
                    send_approval_sms(li_post_id, f"[LINKEDIN] {li_text[:200]}")
                except Exception as e:
                    print(f"Error sending SMS for LinkedIn: {e}")

            if _agent:
                try:
                    _agent.log_action("linkedin_post_drafted", {
                        "post_id": li_post_id,
                        "platform": "linkedin",
                        "char_count": len(li_text),
                        "content_preview": li_text[:80],
                        "status": "HELD" if not li_passed else "DRAFT",
                        "trust_score": li_trust,
                    })
                except Exception:
                    pass
        except Exception as e:
            print(f"LinkedIn generation error (non-blocking): {e}")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "x_post_id": x_post_id,
            "x_post_text": post_text,
            "x_trust_score": x_trust,
            "x_status": "HELD" if not x_passed else "DRAFT",
            "linkedin_post_id": li_post_id,
            "linkedin_trust_score": li_trust,
            "linkedin_enabled": LINKEDIN_ENABLED,
        }),
    }
