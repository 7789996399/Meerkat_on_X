#!/bin/bash
# ============================================================
# Meerkat Labs — X Agent Deployment Script
# Run this step by step (not all at once)
# Each section has a comment explaining what it does
# ============================================================

# ---- CONFIGURATION ----
# Change these to match your setup
REGION="us-east-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
PROJECT_NAME="meerkat"

echo "Deploying to AWS Account: $ACCOUNT_ID in $REGION"

# ============================================================
# STEP 1: Store your API keys securely in Secrets Manager
# ============================================================
# Replace the placeholder values with your REAL keys
# Run this ONCE — it creates a secure vault for your keys

aws secretsmanager create-secret \
  --name meerkat-api-keys \
  --region $REGION \
  --secret-string '{
    "newsapi_key": "YOUR_NEWSAPI_KEY_HERE",
    "x_api_key": "YOUR_X_API_KEY_HERE",
    "x_api_secret": "YOUR_X_API_SECRET_HERE",
    "x_access_token": "YOUR_X_ACCESS_TOKEN_HERE",
    "x_access_token_secret": "YOUR_X_ACCESS_TOKEN_SECRET_HERE"
  }'

echo "✅ Step 1 done: Secrets stored"

# ============================================================
# STEP 2: Create DynamoDB tables
# ============================================================
# Three tables: news, posts, and reply tracking

# News table — stores every article we find
aws dynamodb create-table \
  --table-name meerkat-news \
  --attribute-definitions AttributeName=news_id,AttributeType=S \
  --key-schema AttributeName=news_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region $REGION

# Posts table — stores every draft and published post
aws dynamodb create-table \
  --table-name meerkat-posts \
  --attribute-definitions AttributeName=post_id,AttributeType=S \
  --key-schema AttributeName=post_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region $REGION

# Replies table -- tracks tweets we've seen/replied to (dedup)
aws dynamodb create-table \
  --table-name meerkat-replies \
  --attribute-definitions AttributeName=tweet_id,AttributeType=S \
  --key-schema AttributeName=tweet_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region $REGION

echo "✅ Step 2 done: DynamoDB tables created"

# ============================================================
# STEP 3: Create SNS topic for SMS notifications
# ============================================================

# Create the topic
SNS_TOPIC_ARN=$(aws sns create-topic \
  --name meerkat-approvals \
  --region $REGION \
  --query TopicArn --output text)

echo "SNS Topic ARN: $SNS_TOPIC_ARN"

# Subscribe your phone number (CHANGE THIS to your real number)
# Format: country code + number, e.g., +16045551234
aws sns subscribe \
  --topic-arn $SNS_TOPIC_ARN \
  --protocol sms \
  --notification-endpoint "+1YOURNUMBERHERE" \
  --region $REGION

echo "✅ Step 3 done: SNS topic created. Check your phone for confirmation."

# ============================================================
# STEP 4: Create IAM role for Lambda functions
# ============================================================

# Create the trust policy (allows Lambda to use this role)
cat > /tmp/lambda-trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

# Create the role
aws iam create-role \
  --role-name meerkat-lambda-role \
  --assume-role-policy-document file:///tmp/lambda-trust-policy.json

# Create permissions policy (what the Lambda functions can do)
cat > /tmp/lambda-permissions.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:${REGION}:${ACCOUNT_ID}:*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "dynamodb:Query",
        "dynamodb:Scan"
      ],
      "Resource": [
        "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/meerkat-news",
        "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/meerkat-posts",
        "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/meerkat-replies"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:meerkat-api-keys*"
    },
    {
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": "sns:Publish",
      "Resource": "${SNS_TOPIC_ARN}"
    },
    {
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:meerkat-*"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name meerkat-lambda-role \
  --policy-name meerkat-lambda-permissions \
  --policy-document file:///tmp/lambda-permissions.json

echo "✅ Step 4 done: IAM role created"

# Wait for role to propagate
echo "Waiting 10 seconds for role to propagate..."
sleep 10

# ============================================================
# STEP 5: Package and deploy Lambda functions
# ============================================================

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/meerkat-lambda-role"

# --- Meerkat SDK integration ---
# Each Lambda zip includes the vendored meerkat_sdk/ package.
# Set these env vars in each Lambda to enable SDK integration:
#   MEERKAT_API_URL   -- production: https://api.meerkatplatform.com
#   MEERKAT_AGENT_ID  -- from Meerkat agent registry
#   MEERKAT_API_KEY   -- org API key (mk_live_*)
# If not set, SDK calls are silently skipped.
# --- Meerkat SDK integration ---
# Each Lambda zip includes the vendored meerkat_sdk/ package + requests.
# Set these env vars in each Lambda to enable SDK integration:
#   MEERKAT_API_URL   -- production: https://api.meerkatplatform.com
#   MEERKAT_AGENT_ID  -- from Meerkat agent registry
#   MEERKAT_API_KEY   -- org API key (mk_live_*)
# If not set, SDK calls are silently skipped.

# Install requests into a temp directory for bundling with Lambda zips
rm -rf /tmp/meerkat-deps
pip install -t /tmp/meerkat-deps requests -q 2>/dev/null

# Helper: build a Lambda zip with the SDK and requests included
build_lambda_zip() {
  local lambda_dir=$1
  local zip_name=$2
  rm -f /tmp/${zip_name}.zip
  # Lambda handler
  zip -j /tmp/${zip_name}.zip lambdas/${lambda_dir}/lambda_function.py
  # Vendored SDK
  zip -r /tmp/${zip_name}.zip meerkat_sdk/ -x '*__pycache__*' -q
  # requests + dependencies
  cd /tmp/meerkat-deps && zip -r /tmp/${zip_name}.zip . -x '__pycache__/*' '*.dist-info/*' -q && cd -
}

# --- News Fetcher ---
build_lambda_zip news_fetcher news_fetcher
aws lambda create-function \
  --function-name meerkat-news-fetcher \
  --runtime python3.12 \
  --handler lambda_function.lambda_handler \
  --role $ROLE_ARN \
  --zip-file fileb:///tmp/news_fetcher.zip \
  --timeout 30 \
  --memory-size 256 \
  --environment "Variables={NEWS_TABLE=meerkat-news,GENERATOR_FUNCTION=meerkat-post-generator}" \
  --region $REGION

# --- Post Generator ---
build_lambda_zip post_generator post_generator
aws lambda create-function \
  --function-name meerkat-post-generator \
  --runtime python3.12 \
  --handler lambda_function.lambda_handler \
  --role $ROLE_ARN \
  --zip-file fileb:///tmp/post_generator.zip \
  --timeout 60 \
  --memory-size 256 \
  --environment "Variables={POSTS_TABLE=meerkat-posts,MODEL_ID=us.anthropic.claude-3-5-sonnet-20241022-v2:0,SNS_TOPIC_ARN=${SNS_TOPIC_ARN},APPROVAL_URL=WILL_UPDATE_AFTER_API_GATEWAY}" \
  --region $REGION

# --- Post Publisher ---
build_lambda_zip post_publisher post_publisher
aws lambda create-function \
  --function-name meerkat-post-publisher \
  --runtime python3.12 \
  --handler lambda_function.lambda_handler \
  --role $ROLE_ARN \
  --zip-file fileb:///tmp/post_publisher.zip \
  --timeout 30 \
  --memory-size 256 \
  --environment "Variables={POSTS_TABLE=meerkat-posts}" \
  --region $REGION

# --- Reply Scanner ---
# NOTE: Requires X API Basic plan ($100/mo) for tweet read access
build_lambda_zip reply_scanner reply_scanner
aws lambda create-function \
  --function-name meerkat-reply-scanner \
  --runtime python3.12 \
  --handler lambda_function.lambda_handler \
  --role $ROLE_ARN \
  --zip-file fileb:///tmp/reply_scanner.zip \
  --timeout 120 \
  --memory-size 256 \
  --environment "Variables={REPLIES_TABLE=meerkat-replies,MODEL_ID=us.anthropic.claude-3-5-sonnet-20241022-v2:0,SNS_TOPIC_ARN=${SNS_TOPIC_ARN},APPROVAL_URL=WILL_UPDATE_AFTER_API_GATEWAY,TARGET_ACCOUNTS=AnthropicAI\,OpenAI\,GoogleDeepMind\,MetaAI\,MistralAI\,ai_risks\,AISafetyInst\,FLI_org\,GaryMarcus\,DarioAmodei,LOOKBACK_MINUTES=90,MAX_REPLIES_PER_RUN=3}" \
  --region $REGION

echo "✅ Step 5 done: Lambda functions deployed"

# ============================================================
# STEP 6: Create API Gateway (approval endpoint)
# ============================================================

# Create the API
API_ID=$(aws apigatewayv2 create-api \
  --name meerkat-approval-api \
  --protocol-type HTTP \
  --region $REGION \
  --query ApiId --output text)

echo "API Gateway ID: $API_ID"

# Create the integration (connect API Gateway to the publisher Lambda)
INTEGRATION_ID=$(aws apigatewayv2 create-integration \
  --api-id $API_ID \
  --integration-type AWS_PROXY \
  --integration-uri "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:meerkat-post-publisher" \
  --payload-format-version "2.0" \
  --region $REGION \
  --query IntegrationId --output text)

# Create the route (GET /approve?id=xxx&action=approve)
aws apigatewayv2 create-route \
  --api-id $API_ID \
  --route-key "GET /approve" \
  --target "integrations/$INTEGRATION_ID" \
  --region $REGION

# Create a stage (makes it live)
aws apigatewayv2 create-stage \
  --api-id $API_ID \
  --stage-name prod \
  --auto-deploy \
  --region $REGION

# Allow API Gateway to invoke the Lambda
aws lambda add-permission \
  --function-name meerkat-post-publisher \
  --statement-id apigateway-invoke \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*" \
  --region $REGION

APPROVAL_URL="https://${API_ID}.execute-api.${REGION}.amazonaws.com/prod/approve"
echo "Approval URL: $APPROVAL_URL"

# Update the post generator with the real approval URL
aws lambda update-function-configuration \
  --function-name meerkat-post-generator \
  --environment "Variables={POSTS_TABLE=meerkat-posts,MODEL_ID=us.anthropic.claude-3-5-sonnet-20241022-v2:0,SNS_TOPIC_ARN=${SNS_TOPIC_ARN},APPROVAL_URL=${APPROVAL_URL}}" \
  --region $REGION

# Update the reply scanner with the real approval URL
aws lambda update-function-configuration \
  --function-name meerkat-reply-scanner \
  --environment "Variables={REPLIES_TABLE=meerkat-replies,MODEL_ID=us.anthropic.claude-3-5-sonnet-20241022-v2:0,SNS_TOPIC_ARN=${SNS_TOPIC_ARN},APPROVAL_URL=${APPROVAL_URL},TARGET_ACCOUNTS=AnthropicAI\,OpenAI\,GoogleDeepMind\,MetaAI\,MistralAI\,ai_risks\,AISafetyInst\,FLI_org\,GaryMarcus\,DarioAmodei,LOOKBACK_MINUTES=90,MAX_REPLIES_PER_RUN=3}" \
  --region $REGION

echo "✅ Step 6 done: API Gateway created"

# ============================================================
# STEP 7: Create EventBridge schedule (3x daily)
# ============================================================

# Morning scan (8 AM EST = 13:00 UTC)
aws events put-rule \
  --name meerkat-morning-scan \
  --schedule-expression "cron(0 13 * * ? *)" \
  --state ENABLED \
  --region $REGION

aws events put-targets \
  --rule meerkat-morning-scan \
  --targets "Id=news-fetcher,Arn=arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:meerkat-news-fetcher" \
  --region $REGION

aws lambda add-permission \
  --function-name meerkat-news-fetcher \
  --statement-id eventbridge-morning \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/meerkat-morning-scan" \
  --region $REGION

# Midday scan (12 PM EST = 17:00 UTC)
aws events put-rule \
  --name meerkat-midday-scan \
  --schedule-expression "cron(0 17 * * ? *)" \
  --state ENABLED \
  --region $REGION

aws events put-targets \
  --rule meerkat-midday-scan \
  --targets "Id=news-fetcher,Arn=arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:meerkat-news-fetcher" \
  --region $REGION

aws lambda add-permission \
  --function-name meerkat-news-fetcher \
  --statement-id eventbridge-midday \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/meerkat-midday-scan" \
  --region $REGION

# Evening scan (6 PM EST = 23:00 UTC)
aws events put-rule \
  --name meerkat-evening-scan \
  --schedule-expression "cron(0 23 * * ? *)" \
  --state ENABLED \
  --region $REGION

aws events put-targets \
  --rule meerkat-evening-scan \
  --targets "Id=news-fetcher,Arn=arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:meerkat-news-fetcher" \
  --region $REGION

aws lambda add-permission \
  --function-name meerkat-news-fetcher \
  --statement-id eventbridge-evening \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/meerkat-evening-scan" \
  --region $REGION

# Reply scanner (every 45 minutes, 7 AM - 11 PM EST = 12:00 - 04:00+1 UTC)
# Requires X API Basic plan for read access
aws events put-rule \
  --name meerkat-reply-scan \
  --schedule-expression "rate(45 minutes)" \
  --state ENABLED \
  --region $REGION

aws events put-targets \
  --rule meerkat-reply-scan \
  --targets "Id=reply-scanner,Arn=arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:meerkat-reply-scanner" \
  --region $REGION

aws lambda add-permission \
  --function-name meerkat-reply-scanner \
  --statement-id eventbridge-reply-scan \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/meerkat-reply-scan" \
  --region $REGION

echo "✅ Step 7 done: Schedules set (news: 8AM/12PM/6PM EST, replies: every 45min)"

# ============================================================
# DONE!
# ============================================================
echo ""
echo "============================================"
echo "  MEERKAT X AGENT — DEPLOYMENT COMPLETE"
echo "============================================"
echo ""
echo "  Approval URL: $APPROVAL_URL"
echo "  SNS Topic: $SNS_TOPIC_ARN"
echo "  News schedules: 8 AM, 12 PM, 6 PM EST"
echo "  Reply scanner: every 45 minutes"
echo ""
echo "  To test news fetcher:"
echo "  aws lambda invoke --function-name meerkat-news-fetcher --region $REGION /tmp/test-output.json && cat /tmp/test-output.json"
echo ""
echo "  To test reply scanner:"
echo "  aws lambda invoke --function-name meerkat-reply-scanner --region $REGION /tmp/test-reply.json && cat /tmp/test-reply.json"
echo ""
echo "  IMPORTANT: Reply scanner requires X API Basic plan (\$100/mo)."
echo "  Check your tier at https://developer.x.com/en/portal/products"
echo ""
echo "  The meerkat is on watch. 🦡"
echo "============================================"
