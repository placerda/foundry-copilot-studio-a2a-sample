from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from config import Settings
from errors import ChatFailure


class FakeConverse:
    def __init__(self, output=None, failure=None):
        self.calls = []
        self.output = output
        self.failure = failure

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        return self.output


def model_output(reason="end_turn", text="Olá"):
    return {"stopReason": reason, "output": {"message": {"role": "assistant",
            "content": [{"text": text}, {"text": " mundo"}]}}}


@pytest.mark.parametrize("reason,limited", [("end_turn", False), ("stop_sequence", False), ("max_tokens", True)])
def test_graph_metadata_and_isolation(reason, limited, environment):
    from bedrock_client import Model
    from graph import build_graph
    fake = FakeConverse(model_output(reason))
    graph = build_graph(Model(Settings.from_env(environment), fake))
    for text in ("primeira", "segunda"):
        result = graph.invoke({"messages": [{"role": "user", "content": text}],
                               "remaining_ms": 25000})
        assert result["answer"] == "Olá mundo"
        assert result["output_limited"] is limited
        assert fake.calls[-1]["messages"] == [{"role": "user", "content": [{"text": text}]}]
    assert len(fake.calls) == 2
    assert graph.checkpointer is None
    request = fake.calls[0]
    assert request["modelId"] == "amazon.nova-lite-v1:0"
    assert request["inferenceConfig"] == {"maxTokens": 400, "temperature": 0.3}
    assert request["system"] == [{
        "text": "You are a personal assistant. Respond in English clearly and concisely."
    }]


@pytest.mark.parametrize("output", [
    {}, model_output(None), model_output("tool_use"), model_output("unknown"),
    {"stopReason": "max_tokens", "output": {"message": {"role": "assistant", "content": [{"text": " \n"}]}}},
    {"stopReason": "end_turn", "output": {"message": {"role": "assistant", "content": [{"toolUse": {}}]}}},
    {"stopReason": "end_turn", "output": {"message": {"role": "user", "content": [{"text": "x"}]}}},
    {"stopReason": "end_turn", "output": {"message": {"role": "assistant", "content": [{"text": ""}]}}},
])
def test_invalid_model_output(output, environment):
    from bedrock_client import Model
    fake = FakeConverse(output)
    with pytest.raises(ChatFailure) as error:
        Model(Settings.from_env(environment), fake).invoke([{"role": "user", "content": "x"}], 25000)
    assert error.value.status == 502
    assert len(fake.calls) == 1


@pytest.mark.parametrize("failure,status", [
    (ClientError({"Error": {"Code": "ThrottlingException", "Message": "SECRET"}}, "Converse"), 429),
    (ClientError({"Error": {"Code": "AccessDeniedException", "Message": "SECRET"}}, "Converse"), 502),
    (ReadTimeoutError(endpoint_url="SECRET"), 502),
])
def test_model_failure(failure, status, environment):
    from bedrock_client import Model
    fake = FakeConverse(failure=failure)
    with pytest.raises(ChatFailure) as error:
        Model(Settings.from_env(environment), fake).invoke([], 25000)
    assert error.value.status == status
    assert len(fake.calls) == 1
    assert "SECRET" not in str(error.value)


def test_insufficient_budget(environment):
    from bedrock_client import Model
    fake = FakeConverse()
    with pytest.raises(ChatFailure):
        Model(Settings.from_env(environment), fake).invoke([], 19000)
    assert not fake.calls


def test_sdk_configuration(environment, monkeypatch):
    import bedrock_client
    captured = {}
    def client(service, **kwargs):
        captured.update(kwargs)
        assert service == "bedrock-runtime"
        return SimpleNamespace()
    monkeypatch.setattr(bedrock_client.boto3, "client", client)
    bedrock_client.Model(Settings.from_env(environment))
    assert captured["region_name"] == "us-east-2"
    assert captured["config"].connect_timeout == 2
    assert captured["config"].read_timeout == 18
    assert captured["config"].retries["total_max_attempts"] == 1
