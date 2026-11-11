import json

import pytest

from config import Settings
from errors import ChatFailure


def event_for(messages, **changes):
    return {"body": json.dumps({"messages": messages}, ensure_ascii=False).encode("utf-8"),
            "headers": {"content-type": "application/json"}, **changes}


def test_transport_and_unicode(environment):
    from validation import decode_json, validate_messages
    settings = Settings.from_env(environment)
    for length in (3999, 4000):
        message = [{"role": "user", "content": "😀" * length}]
        event = event_for(message)
        assert validate_messages(decode_json(event["body"], event["headers"]), settings) == message


@pytest.mark.parametrize("size,accepted", [(32767, True), (32768, True), (32769, False)])
def test_exact_body_bytes(size, accepted):
    from validation import decode_json
    raw = '{"messages":[]}'
    event = {"body": (raw + " " * (size - len(raw))).encode(),
             "headers": {"content-type": "application/json"}}
    if accepted:
        assert decode_json(event["body"], event["headers"]) == {"messages": []}
    else:
        with pytest.raises(ChatFailure):
            decode_json(event["body"], event["headers"])


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
    {"body": b"not json"}, {"body": b"null"}, {"body": b'{"messages":[],"extra":1}'},
    {"body": b'{"messages":[],"messages":[]}'},
    {"body": b"\xff"}, {"body": b'{"messages":NaN}'}, {"body": b"[]"},
    {"headers": {"content-type": "text/plain"}},
    {"headers": {"content-type": "application/json", "content-encoding": "gzip"}},
])
def test_invalid_transport(changes):
    from validation import decode_json
    event = event_for([], **changes)
    with pytest.raises(ChatFailure):
        decode_json(event["body"], event["headers"])
