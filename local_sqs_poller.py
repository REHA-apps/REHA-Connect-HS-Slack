import time

import boto3

from app.core.config import settings
from app.sqs_worker import handler


def poll():
    sqs = boto3.client("sqs")
    queue_url = settings.SQS_SLACK_WEBHOOK_QUEUE_URL
    print(f"[*] Starting local SQS poller for: {queue_url}")

    while True:
        try:
            response = sqs.receive_message(
                QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=10
            )

            messages = response.get("Messages", [])
            if messages:
                print(f"[*] Received {len(messages)} messages. Processing...")

                # Format to match AWS Lambda event
                event = {
                    "Records": [
                        {
                            "messageId": m["MessageId"],
                            "receiptHandle": m["ReceiptHandle"],
                            "body": m["Body"],
                        }
                        for m in messages
                    ]
                }

                # Execute the handler just like Lambda does
                from typing import Any, cast

                handler(cast(Any, event), None)

                # Delete messages after successful execution
                for m in messages:
                    sqs.delete_message(
                        QueueUrl=queue_url, ReceiptHandle=m["ReceiptHandle"]
                    )
                print("[*] Batch processed successfully.\n")

        except KeyboardInterrupt:
            print("\n[*] Stopping poller.")
            break
        except Exception as e:
            print(f"[!] Polling error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    poll()
