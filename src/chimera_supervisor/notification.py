# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""Notifier implementations (the protocol lives in core.context).

The supervisor works with any object implementing
:class:`~chimera_supervisor.core.context.Notifier`; production uses
:class:`~chimera_supervisor.telegrambot.TelegramNotifier`.
"""

import datetime
import logging


class NullNotifier:
    """Log-only notifier: used when no Telegram token is configured.
    Questions are answered "no" (fail-safe)."""

    def __init__(self, log: logging.Logger | None = None):
        self.log = log or logging.getLogger(__name__)

    def broadcast(self, message: str) -> None:
        self.log.info("[broadcast] %s", message)

    def broadcast_photo(self, url: str, message: str = "") -> None:
        self.log.info("[broadcast photo] %s %s", url, message)

    def ask(self, question: str, timeout: datetime.timedelta) -> str:
        self.log.info("[ask, nobody listening] %s -> no", question)
        return "no"


class RecordingNotifier(NullNotifier):
    """Notifier that records everything; answers questions from a queue
    (defaults to "no").  Meant for tests."""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []
        self.photos: list[tuple[str, str]] = []
        self.questions: list[str] = []
        self.answers: list[str] = []

    def broadcast(self, message: str) -> None:
        self.messages.append(message)

    def broadcast_photo(self, url: str, message: str = "") -> None:
        self.photos.append((url, message))

    def ask(self, question: str, timeout: datetime.timedelta) -> str:
        self.questions.append(question)
        return self.answers.pop(0) if self.answers else "no"
