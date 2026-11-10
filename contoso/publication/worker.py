"""Fixed publication commands for an explicitly authorized private Linux host."""

import argparse
import json
import logging
import os

from azure.core.exceptions import AzureError
from requests.exceptions import RequestException

from .executor import PublicationFailure, SERVICE_NAMES, _user_agent
from .runtime import create_executor, operator_config


def process_user_agent(value):
    _user_agent(value)
    return value


def parser():
    command = argparse.ArgumentParser(
        description="Fixed-target CONTOSO private publication worker.", allow_abbrev=False,
    )
    operations = command.add_subparsers(dest="operation", required=True)
    for name in ("preflight", "sources", "records", "metadata", "diagnostics", "publish-service"):
        operation = operations.add_parser(name, allow_abbrev=False)
        if name == "publish-service":
            operation.add_argument("--service", required=True, choices=sorted(SERVICE_NAMES))
        if name in ("diagnostics", "publish-service"):
            operation.add_argument("--process-user-agent", required=True, type=process_user_agent)
    return command


def main(argv=None, *, env=None, executor_factory=None):
    args = parser().parse_args(argv)
    logger = logging.getLogger("contoso.publication.worker")
    try:
        executor = (
            executor_factory() if executor_factory is not None
            else create_executor(operator_config(os.environ if env is None else env))
        )
        if args.operation == "preflight":
            executor.preflight()
            result = {"status": "private-preflight-passed"}
        elif args.operation == "sources":
            result = executor.publish_sources()
        elif args.operation == "records":
            result = executor.get_agent_records()
        elif args.operation == "metadata":
            result = {
                "record_contract": 1, "endpoint_validation": "not_requested",
                "records": executor.get_agent_records(include_endpoint=False),
            }
        elif args.operation == "diagnostics":
            result = executor.diagnostic_self_test(process_user_agent=args.process_user_agent)
        else:
            result = executor.publish_service(
                args.service, process_user_agent=args.process_user_agent,
            ).as_dict()
        failed = result.get("status") == "failed"
        if failed:
            logger.error("CONTOSO private publication worker failed: %s", args.operation)
        print(json.dumps(result, ensure_ascii=True))
        return 1 if failed else 0
    except PublicationFailure as error:
        code = error.code
    except (AzureError, RequestException):
        code = "private_dependency_unavailable"
    except (ValueError, KeyError, OSError):
        code = "invalid_publication_configuration_or_operation"
    logger.error("CONTOSO private publication worker failed: %s", code)
    print(json.dumps({"status": "failed", "error": code}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
