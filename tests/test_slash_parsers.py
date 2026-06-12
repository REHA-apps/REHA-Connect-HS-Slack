from datetime import UTC, datetime, timedelta  # noqa: D100

from app.utils.parsers import parse_reha_task_command as parse_hs_task_command


def test_parse_hs_task_simple():
    text = "Call John"
    result = parse_hs_task_command(text)
    assert result["subject"] == "Call John"
    assert result["slack_user_id"] is None
    assert result["due_date"] is None


def test_parse_hs_task_with_mention():
    text = "Call John <@U12345>"
    result = parse_hs_task_command(text)
    assert result["subject"] == "Call John"
    assert result["slack_user_id"] == "U12345"
    assert result["due_date"] is None


def test_parse_hs_task_with_mention_first():
    text = "<@U12345> Call John"
    result = parse_hs_task_command(text)
    assert result["subject"] == "Call John"
    assert result["slack_user_id"] == "U12345"
    assert result["due_date"] is None


def test_parse_hs_task_with_today():
    text = "Call John today"
    result = parse_hs_task_command(text)
    assert result["subject"] == "Call John"
    assert result["due_date"] is not None
    assert result["due_date"].date() == datetime.now(UTC).date()


def test_parse_hs_task_with_tomorrow():
    text = "Call John tomorrow"
    result = parse_hs_task_command(text)
    assert result["subject"] == "Call John"
    assert result["due_date"] is not None
    assert result["due_date"].date() == (datetime.now(UTC) + timedelta(days=1)).date()


def test_parse_hs_task_full():
    text = "Call John <@U12345> next week"
    result = parse_hs_task_command(text)
    assert result["subject"] == "Call John"
    assert result["slack_user_id"] == "U12345"
    assert result["due_date"] is not None
    assert result["due_date"].date() == (datetime.now(UTC) + timedelta(weeks=1)).date()


def test_parse_hs_task_weird_spacing():
    text = "  Call   John   <@U12345>   tomorrow  "
    result = parse_hs_task_command(text)
    assert result["subject"] == "Call John"
    assert result["slack_user_id"] == "U12345"
    assert result["due_date"] is not None
