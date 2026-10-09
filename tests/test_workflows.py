"""Run lifecycle helpers that need no database."""

from __future__ import annotations

import builtins
import logging

import pydantic_ai.exceptions
from dbos._error import DBOSAwaitedWorkflowCancelledError, DBOSException

from sammy.browser import cdp_client, contract, service
from sammy.vendor.claude_code import auth as claude_code_auth
from sammy.workflows import FAILURE_NOTICE, FAILURE_NOTICES, HideStoppedRuns, failure_notice


def record(error: BaseException) -> logging.LogRecord:
    return logging.LogRecord('dbos', logging.ERROR, __file__, 1, 'failed', None, (type(error), error, None))


def test_a_stopped_run_is_not_logged_as_an_error() -> None:
    hide = HideStoppedRuns()
    assert not hide.filter(record(DBOSAwaitedWorkflowCancelledError('run-1')))
    assert hide.filter(record(DBOSException('a real failure')))
    assert hide.filter(logging.LogRecord('dbos', logging.INFO, __file__, 1, 'launched', None, None))


def test_a_failure_says_why_in_words() -> None:
    """Each error type with its own notice is a real exception's name; any other gets the general notice."""
    modules = [builtins, pydantic_ai.exceptions, cdp_client, contract, service, claude_code_auth]
    for name in FAILURE_NOTICES:
        assert any(isinstance(getattr(module, name, None), type) for module in modules), name
    assert failure_notice(pydantic_ai.exceptions.UsageLimitExceeded('too many')) != FAILURE_NOTICE
    # A type without a notice of its own gets its parent's: an incomplete tool call is the model misbehaving.
    incomplete = pydantic_ai.exceptions.IncompleteToolCall('cut off')
    assert failure_notice(incomplete) == FAILURE_NOTICES['UnexpectedModelBehavior']
    assert failure_notice(service.UserBusy('busy')) == FAILURE_NOTICES['UserBusy']  # not just any BrowserError
    assert failure_notice(RuntimeError('boom')) == FAILURE_NOTICE
