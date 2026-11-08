"""A cancelled caller cannot strand a queued worker's admission slot."""

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
import threading
from typing import TypeVar

from contoso.providers import ProviderTimeout, TransportError


T = TypeVar("T")


async def run_bounded_sync(slot: threading.Semaphore, work: Callable[[], T]) -> T:
    if not slot.acquire(blocking=False):
        raise ProviderTimeout("The configured transport is busy")
    result: Future[T] = Future()
    result.set_running_or_notify_cancel()

    def run():
        try:
            value = work()
        except BaseException as error:
            result.set_exception(error)
        else:
            result.set_result(value)
        finally:
            slot.release()

    try:
        threading.Thread(target=run, daemon=True).start()
    except RuntimeError:
        slot.release()
        raise TransportError("The bounded transport worker could not start") from None
    return await asyncio.wrap_future(result)
