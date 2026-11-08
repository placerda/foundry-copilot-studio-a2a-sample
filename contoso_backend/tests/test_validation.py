import base64
import json

import pytest

from config import Settings
from errors import ChatFailure


def event_for(messages, **changes):
    return {"body": json.dumps({"messages": messages}, ensure_ascii=False),
            "headers": {"content-type": "application/json"}, **changes}


def test_transport_and_unicode(environment):
    from validation import decode_transport, validate_messages
    settings = Settings.from_env(environment)
    for length in (3999, 4000):
        message = [{"role": "user", "content": "😀" * length}]
        event = event_for(message)
        assert validate_messages(decode_transport(event), settings) == message
        event["body"] = base64.b64encode(event["body"].encode()).decode()
        event["isBase64Encoded"] = True
        assert validate_messages(decode_transport(event), settings) == message


@pytest.mark.parametrize("size,accepted", [(32767, True), (32768, True), (32769, False)])
def test_exact_body_bytes(size, accepted):
    from validation import decode_transport
    raw = '{"messages":[]}'
    event = {"body": raw + " " * (size - len(raw)), "headers": {"content-type": "application/json"}}
    if accepted:
        assert decode_transport(event) == {"messages": []}
    else:
        with pytest.raises(ChatFailure):
            decode_transport(event)


@pytest.mark.parametrize("messages", [
    [], [{"role": "user", "content": "x" * 4001}],
    [{"role": "user", "content": " \n\t"}],
    [{"role": "user", "content": "\ud800"}],
    [{"role": "system", "content": "x"}],
    [{"role": "tool", "content": "x"}],
    [{"role": "assistant", "content": "x"}],
    [{"role": "user", "content": "x", "extra": True}],
    [{"role": "user", "content": 4}],
    [{"role": "user", "content": "x"}, {"role": "user", "content": "x"},
     {"role": "user", "content": "x"}],
])
def test_invalid_messages(messages, environment):
    from validation import validate_messages
    with pytest.raises(ChatFailure):
        validate_messages({"messages": messages}, Settings.from_env(environment))


@pytest.mark.parametrize("count,accepted", [(0, False), (1, True), (9, True), (10, False), (11, False)])
def test_list_limits(count, accepted, environment):
    from validation import validate_messages
    payload = {"messages": [{"role": "user" if i % 2 == 0 else "assistant", "content": "x"}
                            for i in range(count)]}
    if accepted:
        assert len(validate_messages(payload, Settings.from_env(environment))) == count
    else:
        with pytest.raises(ChatFailure):
            validate_messages(payload, Settings.from_env(environment))


@pytest.mark.parametrize("changes", [
    {"body": "not json"}, {"body": "null"}, {"body": '{"messages":[],"extra":1}'},
    {"body": '{"messages":[],"messages":[]}'},
    {"body": "\ud800"}, {"body": "not base64", "isBase64Encoded": True},
    {"body": "/w==", "isBase64Encoded": True},
    {"headers": {"content-type": "text/plain"}},
    {"headers": {"content-type": "application/json", "content-encoding": "gzip"}},
])
def test_invalid_transport(changes):
    from validation import decode_transport
    with pytest.raises(ChatFailure):
        decode_transport(event_for([], **changes))
