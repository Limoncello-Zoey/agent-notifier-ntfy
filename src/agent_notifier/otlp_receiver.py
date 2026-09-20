"""Minimal OTLP/HTTP JSON log decoder for Codex transport events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


SUPPORTED_EVENTS = {
    "codex.api_request": "api",
    "codex.sse_event": "sse",
    "codex.websocket_request": "websocket",
    "codex.websocket_event": "websocket",
}


class OtlpDecodeError(ValueError):
    """An OTLP JSON request does not have the expected envelope shape."""


@dataclass(frozen=True, slots=True)
class TransportEvent:
    name: str
    session_id: str
    transport: str
    success: bool
    model: str | None = None
    status: int | None = None
    attempt: int | None = None
    error: str | None = None


def decode_otlp_logs(payload: object) -> list[TransportEvent]:
    if not isinstance(payload, dict):
        raise OtlpDecodeError("OTLP JSON 根节点必须是对象")
    resource_logs = payload.get("resourceLogs", payload.get("resource_logs"))
    if not isinstance(resource_logs, list):
        raise OtlpDecodeError("OTLP JSON 缺少 resourceLogs 数组")

    events: list[TransportEvent] = []
    for resource_item in resource_logs:
        if not isinstance(resource_item, dict):
            continue
        resource_attrs = _attributes(_mapping(resource_item.get("resource")).get("attributes"))
        scopes = resource_item.get("scopeLogs", resource_item.get("scope_logs", []))
        if not isinstance(scopes, list):
            continue
        for scope_item in scopes:
            if not isinstance(scope_item, dict):
                continue
            records = scope_item.get("logRecords", scope_item.get("log_records", []))
            if not isinstance(records, list):
                continue
            for record in records:
                event = _decode_record(record, resource_attrs)
                if event is not None:
                    events.append(event)
    return events


def _decode_record(record: object, resource_attrs: Mapping[str, Any]) -> TransportEvent | None:
    if not isinstance(record, dict):
        return None
    attrs = dict(resource_attrs)
    attrs.update(_attributes(record.get("attributes")))
    body = _any_value(record.get("body"))
    if isinstance(body, dict):
        attrs.update(body)

    name = _first_text(attrs, "event.name", "event_name", "name")
    if name is None and isinstance(body, str):
        name = body
    if name not in SUPPORTED_EVENTS:
        return None

    session = _first_text(
        attrs,
        "conversation.id",
        "conversation_id",
        "thread.id",
        "thread_id",
        "session.id",
        "session_id",
    )
    if not session:
        return None
    status = _first_int(attrs, "status", "status_code", "http.status_code", "http.response.status_code")
    explicit_success = _first_bool(attrs, "success", "ok")
    error = _first_text(attrs, "error.message", "error_message", "error", "exception.message")
    event_kind = _first_text(attrs, "event.kind", "event_kind", "kind")
    if explicit_success is not None:
        success = explicit_success
    elif status is not None:
        success = 100 <= status < 400
    elif error:
        success = False
    elif name == "codex.sse_event" and event_kind is not None:
        # Current Codex success records carry event.kind and duration, but no
        # explicit success flag. Failed records carry error.message.
        success = True
    else:
        # An incomplete record provides no reliable failure signal.
        return None

    return TransportEvent(
        name=name,
        session_id=session,
        transport=SUPPORTED_EVENTS[name],
        success=success,
        model=_first_text(attrs, "model", "model.name"),
        status=status,
        attempt=_first_int(attrs, "attempt", "retry_attempt", "request.attempt"),
        error=error,
    )


def _attributes(value: object) -> dict[str, Any]:
    if not isinstance(value, list):
        return {}
    result: dict[str, Any] = {}
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            continue
        result[item["key"]] = _any_value(item.get("value"))
    return result


def _any_value(value: object) -> Any:
    if not isinstance(value, dict):
        return value
    scalar_keys = ("stringValue", "boolValue", "intValue", "doubleValue", "bytesValue")
    for key in scalar_keys:
        if key in value:
            result = value[key]
            if key == "intValue" and isinstance(result, str):
                try:
                    return int(result)
                except ValueError:
                    pass
            return result
    array = value.get("arrayValue")
    if isinstance(array, dict):
        values = array.get("values", [])
        return [_any_value(item) for item in values] if isinstance(values, list) else []
    pairs = value.get("kvlistValue")
    if isinstance(pairs, dict):
        return _attributes(pairs.get("values"))
    # Be liberal when test collectors or older exporters use snake_case.
    for key in ("string_value", "bool_value", "int_value", "double_value", "bytes_value"):
        if key in value:
            return value[key]
    return value


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_text(values: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_int(values: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = values.get(key)
        if type(value) is int:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _first_bool(values: Mapping[str, Any], *keys: str) -> bool | None:
    for key in keys:
        value = values.get(key)
        if type(value) is bool:
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
    return None
