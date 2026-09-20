import pytest

from agent_notifier.otlp_receiver import OtlpDecodeError, decode_otlp_logs


def av(value):
    key = "boolValue" if isinstance(value, bool) else "intValue" if isinstance(value, int) else "stringValue"
    return {key: value}


def attr(key, value):
    return {"key": key, "value": av(value)}


def envelope(records, resource_attributes=None):
    return {
        "resourceLogs": [{
            "resource": {"attributes": resource_attributes or []},
            "scopeLogs": [{"logRecords": records}],
        }]
    }


def test_decodes_supported_event_and_merges_resource_metadata() -> None:
    payload = envelope(
        [{
            "body": av("codex.api_request"),
            "attributes": [
                attr("conversation.id", "conv-1"),
                attr("success", False),
                attr("status", 429),
                attr("attempt", 2),
                attr("error.message", "rate limited"),
            ],
        }],
        [attr("model", "gpt-test")],
    )
    event = decode_otlp_logs(payload)[0]
    assert event.name == "codex.api_request"
    assert event.session_id == "conv-1"
    assert event.transport == "api"
    assert event.model == "gpt-test"
    assert event.status == 429
    assert event.attempt == 2
    assert event.success is False


def test_decodes_event_name_from_attributes_and_kv_body() -> None:
    body = {"kvlistValue": {"values": [attr("success", True), attr("status_code", "200")]}}
    record = {
        "body": body,
        "attributes": [attr("event.name", "codex.websocket_event"), attr("thread_id", "t")],
    }
    event = decode_otlp_logs(envelope([record]))[0]
    assert event.transport == "websocket"
    assert event.status == 200
    assert event.success is True


def test_ignores_unrelated_and_unattributable_records() -> None:
    records = [
        {"body": av("codex.tool_result"), "attributes": [attr("conversation.id", "c")]},
        {"body": av("codex.sse_event"), "attributes": [attr("success", False)]},
        "bad",
    ]
    assert decode_otlp_logs(envelope(records)) == []


def test_ignores_supported_record_without_failure_or_success_signal() -> None:
    record = {
        "body": av("codex.api_request"),
        "attributes": [attr("conversation.id", "c")],
    }
    assert decode_otlp_logs(envelope([record])) == []


@pytest.mark.parametrize("payload", [None, [], {}, {"resourceLogs": {}}])
def test_rejects_invalid_envelope(payload) -> None:
    with pytest.raises(OtlpDecodeError):
        decode_otlp_logs(payload)
