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

## Setup
See `infrastructure/deploy.sh` for step-by-step deployment.
