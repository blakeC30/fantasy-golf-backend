#!/bin/bash
# Bootstrap script run automatically by LocalStack after startup.
# LocalStack executes all scripts in /etc/localstack/init/ready.d/ once the
# service is ready. This script creates the DLQ first, then the main event
# queue with a redrive policy pointing at it.
#
# Queue names match the SQS_QUEUE_URL in docker-compose.yml:
#   fantasy-golf-events-dev      (main queue)
#   fantasy-golf-events-dev-dlq  (dead-letter queue)

set -e

echo "Creating SQS dead-letter queue..."
awslocal sqs create-queue \
  --queue-name fantasy-golf-events-dev-dlq \
  --region us-east-1

DLQ_ARN=$(awslocal sqs get-queue-attributes \
  --queue-url http://localhost:4566/000000000000/fantasy-golf-events-dev-dlq \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' \
  --output text)

echo "DLQ ARN: ${DLQ_ARN}"

echo "Creating SQS main event queue..."
awslocal sqs create-queue \
  --queue-name fantasy-golf-events-dev \
  --region us-east-1 \
  --attributes "{
    \"VisibilityTimeout\": \"120\",
    \"ReceiveMessageWaitTimeSeconds\": \"20\",
    \"RedrivePolicy\": \"{\\\"deadLetterTargetArn\\\":\\\"${DLQ_ARN}\\\",\\\"maxReceiveCount\\\":\\\"3\\\"}\"
  }"

echo "SQS queues created successfully."
echo "  Main:  http://localhost:4566/000000000000/fantasy-golf-events-dev"
echo "  DLQ:   http://localhost:4566/000000000000/fantasy-golf-events-dev-dlq"

# Verify the SES sender identity so the backend can send password reset emails.
# LocalStack auto-confirms all identities — no real verification email is sent.
echo "Verifying SES sender identity..."
awslocal ses verify-email-identity \
  --email-address noreply@league-caddie.com \
  --region us-east-1
echo "SES sender identity verified: noreply@league-caddie.com"
