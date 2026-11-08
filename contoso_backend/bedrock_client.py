import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from errors import ChatFailure, dependency

SYSTEM = "You are a personal assistant. Respond in English clearly and concisely."


class Model:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client if client is not None else boto3.client(
            "bedrock-runtime", region_name=settings.region,
            config=Config(connect_timeout=2, read_timeout=18,
                          retries={"total_max_attempts": 1, "mode": "standard"}))

    def invoke(self, messages, remaining_ms):
        if remaining_ms < 22000:
            raise dependency()
        try:
            output = self.client.converse(
                modelId=self.settings.model_id,
                messages=[{"role": message["role"], "content": [{"text": message["content"]}]}
                          for message in messages],
                system=[{"text": SYSTEM}],
                inferenceConfig={"maxTokens": self.settings.max_output_tokens, "temperature": 0.3})
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ThrottlingException":
                raise ChatFailure(429, "throttled", "The service is busy. Wait before resending.") from None
            raise dependency() from None
        except BotoCoreError:
            raise dependency() from None
        try:
            reason = output["stopReason"]
            message = output["output"]["message"]
            blocks = message["content"]
            if (reason not in ("end_turn", "stop_sequence", "max_tokens")
                    or message["role"] != "assistant" or not isinstance(blocks, list) or not blocks):
                raise ValueError
            text = []
            for block in blocks:
                if not isinstance(block, dict) or set(block) != {"text"} or not isinstance(block["text"], str):
                    raise ValueError
                text.append(block["text"])
            answer = "".join(text)
            if not answer.strip() or any(0xD800 <= ord(char) <= 0xDFFF for char in answer):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise dependency() from None
        return {"answer": answer, "output_limited": reason == "max_tokens"}
