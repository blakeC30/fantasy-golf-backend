"""
SQS client wrapper — publish and consume.

Reads AWS_ENDPOINT_URL from env; when set (local dev with LocalStack) boto3
uses it instead of the real AWS endpoint. In production this env var is absent
and boto3 uses the real AWS SQS endpoint, picking up credentials from the EC2
instance profile automatically — no access keys stored in environment variables.

Usage (publishing):
    from app.services.sqs import publish
    publish("TOURNAMENT_IN_PROGRESS", tournament_id=str(tournament.id))

Usage (consuming, in worker_main.py):
    from app.services.sqs import consume
    consume(handle)   # blocks forever; call from the worker entrypoint
"""

import json
import logging
import os
from typing import Callable

log = logging.getLogger(__name__)


def _get_client():
    """Create a boto3 SQS client. Called per-operation — boto3 caches the
    underlying HTTP session internally so repeated calls are cheap."""
    kwargs = {"region_name": os.environ["AWS_REGION"]}
    endpoint = os.environ.get("AWS_ENDPOINT_URL")  # set locally, absent in prod
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    import boto3
    return boto3.client("sqs", **kwargs)


def get_queue_url() -> str:
    return os.environ["SQS_QUEUE_URL"]


def publish(event_type: str, **payload) -> None:
    """
    Publish a JSON message to the main event queue.

    event_type is the routing key; **payload contains the event-specific fields.
    Example: publish("TOURNAMENT_IN_PROGRESS", tournament_id="<uuid>")
    """
    client = _get_client()
    body = json.dumps({"type": event_type, **payload})
    client.send_message(QueueUrl=get_queue_url(), MessageBody=body)
    log.info("SQS published: %s %s", event_type, payload)


def consume(handler: Callable[[dict], None]) -> None:
    """
    Long-polling consumer loop. Runs forever — call from worker_main.py.

    Receives up to 10 messages per poll using 20-second long polling (reduces
    empty-receive API calls and cost). Each message is processed by handler()
    then deleted from the queue on success. If handler() raises, the message is
    NOT deleted — it reappears after the visibility timeout and is retried up to
    max_receive_count times before moving to the DLQ.

    handler() receives the parsed message body dict and should raise only for
    unexpected errors that should trigger a retry. Known no-ops (e.g. draft
    already resolved) should be handled silently inside the handler.
    """
    client = _get_client()
    queue_url = get_queue_url()
    log.info("SQS consumer started on %s", queue_url)
    while True:
        response = client.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=20,      # long polling — wait up to 20s for messages
            VisibilityTimeout=120,   # 2× max expected processing time
        )
        for msg in response.get("Messages", []):
            body = json.loads(msg["Body"])
            receipt = msg["ReceiptHandle"]
            try:
                handler(body)
                client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
            except Exception as exc:
                log.error(
                    "SQS handler failed for %s: %s",
                    body.get("type"),
                    exc,
                    exc_info=True,
                )
                # Do NOT delete the message — it reappears after the visibility
                # timeout and will be retried (up to max_receive_count, then DLQ).
