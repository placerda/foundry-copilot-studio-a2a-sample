"""Selected-operator diagnostic using the Gateway's own service identity."""

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity import ManagedIdentityCredential
from openai import APIError
from contoso.identity_probe import parse_link_reply, parse_probe_reply

from contoso.publication import PublicationFailure


SERVICES = frozenset({"contoso-knowledge-agent", "contoso-foundry-sdk-agent", "contoso-foundry-agent"})


def probe_service(config, service, *, link=False):
    if not isinstance(service, str) or service not in SERVICES:
        raise ValueError("Only the three fixed identity-bootstrap services may be probed")
    if link and service == "contoso-knowledge-agent":
        raise ValueError("Only the two Routers support diagnostic linking")
    try:
        with ManagedIdentityCredential() as credential:
            with AIProjectClient(endpoint=config.project_endpoint, credential=credential) as project:
                with project.get_openai_client(agent_name=service) as client:
                    response = client.responses.create(
                        input="CONTOSO identity link probe" if link else "CONTOSO identity probe",
                        store=False, timeout=60,
                    )
        if response.status != "completed" or not response.output:
            raise ValueError("The identity-bootstrap invocation did not complete")
        text = "".join(
            content.text
            for item in response.output if item.type == "message"
            for content in item.content if content.type == "output_text"
        )
        result = parse_link_reply(text) if link else parse_probe_reply(text)
        return {"service": service, "integration": "sdk", **result}
    except (AzureError, APIError, ValueError):
        raise PublicationFailure("identity_probe_unavailable") from None
