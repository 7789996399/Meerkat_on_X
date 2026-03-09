# Meerkat Labs — X/Twitter AI Governance Agent

An automated agent that monitors AI governance news, generates brand-voiced
commentary using Amazon Bedrock (Claude), and posts to X/Twitter with
human-in-the-loop approval via SMS.

## Architecture

```
EventBridge (3x daily schedule)
        │
        ▼
Lambda: news_fetcher ──► NewsAPI / Arxiv / GitHub
        │
        ▼
Lambda: post_generator ──► Bedrock (Claude 3.5 Sonnet)
        │
        ▼
DynamoDB (draft saved) ──► SNS ──► SMS to your phone
        │
        ▼
You reply "APPROVE 001" via API Gateway
        │
        ▼
Lambda: approval_handler ──► X/Twitter API ──► Post published!
```

## AWS Services Used
- **Lambda** — runs the code (serverless, pay-per-use)
- **Bedrock** — Claude writes the posts
- **DynamoDB** — stores all drafts and published posts
- **SNS** — sends SMS notifications
- **EventBridge** — triggers the schedule
- **Secrets Manager** — stores API keys securely
- **API Gateway** — approval endpoint
- **CloudWatch** — logs and monitoring

## Meerkat Console Integration

Each Lambda reports its activity to the Meerkat Console so the Orchestrator
can see what this agent is doing. This is optional and non-blocking.

Set these environment variables on each Lambda function (via AWS Console or deploy.sh):

| Variable | Description | Example |
|----------|-------------|---------|
| `MEERKAT_API_URL` | Meerkat API base URL | `https://api.meerkatplatform.com` |
| `MEERKAT_AGENT_ID` | Agent ID from Console seed data | `agt_general_x_posting_agent_315a2a` |
| `MEERKAT_API_KEY` | Org API key | `mk_live_*` |

If any of these are missing, Console reporting is silently skipped and the
Lambda functions work exactly as before.

## Setup
See `infrastructure/deploy.sh` for step-by-step deployment.
