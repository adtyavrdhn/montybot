"""Run lifecycle helpers that need no database."""

from __future__ import annotations

import builtins
import logging

import pydantic_ai.exceptions
from dbos._error import DBOSAwaitedWorkflowCancelledError, DBOSException

from montybot.browser import cdp_client, contract, service
from montybot.workflows import FAILURE_NOTICE, FAILURE_NOTICES, HideStoppedRuns, failure_notice


def record(error: BaseException) -> logging.LogRecord:
    return logging.LogRecord('dbos', logging.ERROR, __file__, 1, 'failed', None, (type(error), error, None))


def test_a_stopped_run_is_not_logged_as_an_error() -> None:
    hide = HideStoppedRuns()
    assert not hide.filter(record(DBOSAwaitedWorkflowCancelledError('run-1')))
    assert hide.filter(record(DBOSException('a real failure')))
    assert hide.filter(logging.LogRecord('dbos', logging.INFO, __file__, 1, 'launched', None, None))


def test_a_failure_says_why_in_words() -> None:
    """Each error type with its own notice is a real exception's name; any other gets the general notice."""
    modules = [builtins, pydantic_ai.exceptions, cdp_client, contract, service]
    for name in FAILURE_NOTICES:
        assert any(isinstance(getattr(module, name, None), type) for module in modules), name
    assert failure_notice('UsageLimitExceeded') != FAILURE_NOTICE
    assert failure_notice('RuntimeError') == FAILURE_NOTICE
