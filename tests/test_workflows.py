"""Run lifecycle helpers that need no database."""

from __future__ import annotations

import logging

from dbos._error import DBOSAwaitedWorkflowCancelledError, DBOSException

from montybot.workflows import HideStoppedRuns


def record(error: BaseException) -> logging.LogRecord:
    return logging.LogRecord('dbos', logging.ERROR, __file__, 1, 'failed', None, (type(error), error, None))


def test_a_stopped_run_is_not_logged_as_an_error() -> None:
    hide = HideStoppedRuns()
    assert not hide.filter(record(DBOSAwaitedWorkflowCancelledError('run-1')))
    assert hide.filter(record(DBOSException('a real failure')))
    assert hide.filter(logging.LogRecord('dbos', logging.INFO, __file__, 1, 'launched', None, None))
