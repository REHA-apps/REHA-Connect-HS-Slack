from unittest.mock import patch

from app.lambda_handler import handler


def test_http_event_routes_to_mangum():
    """Verify that HTTP events are routed to the Mangum ASGI adapter."""
    with patch("app.lambda_handler._mangum_handler") as mock_mangum:
        event = {
            "requestContext": {"http": {"method": "POST", "path": "/hubspot/webhooks"}},
            "body": "{}",
        }
        handler(event, {})
        mock_mangum.assert_called_once_with(event, {})


def test_scheduler_event_routes_to_scheduler():
    """Verify that background task events are routed to the internal scheduler."""
    # The import in lambda_handler is local: from lambda_scheduler import handler as scheduler_handler
    # So we patch 'lambda_scheduler.handler'
    with patch("lambda_scheduler.handler") as mock_sched:
        event = {"task": "billing"}
        handler(event, {})
        mock_sched.assert_called_once_with(event, {})
