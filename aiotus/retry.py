import asyncio
import dataclasses
from typing import BinaryIO, Callable, Dict, Optional

import aiohttp
import tenacity  # type: ignore
import yarl

from . import common, core, creation
from .log import logger


@dataclasses.dataclass
class RetryConfiguration:
    """Class to hold settings for the functions of this module."""

    # Number of retry attempts to do when the communication fails.
    retry_attempts: int = 10

    # Maximum time between retries, in seconds.
    #
    # Exponential backoff is used in case of communication errors,
    # but the time between retries is caped by this value.
    max_retry_period_seconds: float = 60.0

    # 'ssl' argument passed on to the aiohttp calls.
    #
    # This can be None, False, or an instance of ssl.SSLContext, see
    # https://docs.aiohttp.org/en/stable/client_advanced.html#ssl-control-for-tcp-sockets
    # for the different meanings.
    ssl: common.SSLArgument = None


def _make_log_before_function(s: str) -> Callable[[str], None]:
    def log(retry_state: tenacity.RetryCallState) -> None:

        if retry_state.attempt_number > 1:
            logger.info(
                f"Trying {s} again, attempt number {retry_state.attempt_number}..."
            )

    return log


def _make_log_before_sleep_function(s: str) -> Callable[[str], None]:
    def log(retry_state: tenacity.RetryCallState) -> None:
        duration = retry_state.next_action.sleep
        if retry_state.outcome.failed:
            value = retry_state.outcome.exception()
        else:
            value = retry_state.outcome.result()
        logger.warning(
            f"{s.capitalize()} failed, retrying in {duration:.0f} second(s): {value}"
        )

    return log


async def upload(
    endpoint: str,
    file: BinaryIO,
    metadata: Optional[Dict[str, str]] = None,
    client_session: Optional[aiohttp.ClientSession] = None,
    config: RetryConfiguration = RetryConfiguration(),
) -> Optional[yarl.URL]:
    """Upload a file to a tus server.

    In case of a communication error, this function retries the upload.

    :param endpoint: The creation endpoint of the server.
    :param file: The file to upload.
    :param metadata: Additional metadata for the upload.
    :param config: Settings to customize the upload.
    :return: The location where the file was uploaded to (if the upload succeeded).
    """

    url = yarl.URL(endpoint)

    if metadata is None:
        metadata = {}

    retrying_create = tenacity.AsyncRetrying(
        retry=tenacity.retry_if_exception_type(aiohttp.ClientError),
        stop=tenacity.stop_after_attempt(config.retry_attempts),
        wait=tenacity.wait_exponential(max=config.max_retry_period_seconds),
        before=_make_log_before_function("upload creation"),
        before_sleep=_make_log_before_sleep_function("upload creation"),
    )

    retrying_upload_file = tenacity.AsyncRetrying(
        retry=tenacity.retry_if_exception_type(aiohttp.ClientError),
        stop=tenacity.stop_after_attempt(config.retry_attempts),
        wait=tenacity.wait_exponential(max=config.max_retry_period_seconds),
        before=_make_log_before_function("upload"),
        before_sleep=_make_log_before_sleep_function("upload"),
    )

    try:
        if client_session is None:
            client_session = aiohttp.ClientSession()

        async with client_session:
            location: yarl.URL
            location = await retrying_create.call(
                creation.create, client_session, url, file, metadata, ssl=config.ssl
            )
            if not location.is_absolute():
                location = url / location.path

            await retrying_upload_file.call(
                core.upload_buffer, client_session, location, file, ssl=config.ssl
            )

            return location
    except asyncio.CancelledError:  # pragma: no cover
        # Up until python 3.7, CancelledError is not derived from BaseException.
        raise
    except tenacity.RetryError as e:
        logger.error(
            f"Unable to upload file, even after retrying: {e.last_attempt.exception()}"
        )
    except Exception as e:
        logger.error(f"Unable to upload file: {e}")

    return None