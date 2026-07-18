# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""Telegram transport for the operator interface.

This module contains *only* Telegram-specific plumbing.  What the commands
do lives in :class:`~chimera_supervisor.operator.OperatorCommands`; what the
checklist can say to the operator is the
:class:`~chimera_supervisor.core.context.Notifier` protocol.  A Slack (or
any other chat) integration reimplements this file, nothing else.

Runs python-telegram-bot (v21+, asyncio) on its own event loop in a daemon
thread so the rest of the supervisor stays synchronous.
"""

import asyncio
import datetime
import logging
import ssl
import threading
import urllib.request
import uuid

import telegram
import telegram.ext

from chimera_supervisor.operator import OperatorCommands, Reply, SupervisorPort

_COMMANDS = ("list", "run", "info", "lock", "unlock", "reload", "help")


class TelegramNotifier:
    """Notifier + operator commands over Telegram."""

    def __init__(
        self,
        token: str,
        broadcast_ids: list[int],
        listen_ids: list[int],
        supervisor: SupervisorPort | None = None,
        log: logging.Logger | None = None,
    ):
        self.log = log or logging.getLogger(__name__)
        self._broadcast_ids = list(broadcast_ids)
        self._listen_ids = list(listen_ids)
        self._commands = (
            OperatorCommands(supervisor) if supervisor is not None else None
        )
        self._pending: dict[str, _PendingQuestion] = {}

        self._app = telegram.ext.Application.builder().token(token).build()
        for name in _COMMANDS:
            self._app.add_handler(
                telegram.ext.CommandHandler(name, self._make_command_handler(name))
            )
        self._app.add_handler(telegram.ext.CallbackQueryHandler(self._on_button))

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="telegram-bot", daemon=True
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._startup())
            self._loop.run_forever()
        except Exception:
            self.log.exception("telegram bot loop crashed")

    async def _startup(self) -> None:
        await self._app.initialize()
        await self._app.updater.start_polling()
        await self._app.start()
        self.log.info(
            "telegram bot polling (broadcast: %s, listen: %s)",
            self._broadcast_ids,
            self._listen_ids,
        )

    def stop(self) -> None:
        if not self._thread.is_alive():
            return

        async def _shutdown():
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            finally:
                self._loop.stop()

        asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
        self._thread.join(timeout=10)

    def _submit(self, coroutine, timeout: float = 30.0):
        """Run a coroutine on the bot loop from a foreign thread."""
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=timeout)

    # ------------------------------------------------------------------
    # Notifier protocol
    # ------------------------------------------------------------------

    def broadcast(self, message: str) -> None:
        for chat_id in self._broadcast_ids:
            try:
                self._submit(
                    self._app.bot.send_message(chat_id=chat_id, text=str(message))
                )
            except Exception:
                self.log.exception("could not broadcast to %s", chat_id)

    def broadcast_photo(self, url: str, message: str = "") -> None:
        # observatory cameras live on the local network, so fetch the image
        # here and upload the bytes (Telegram's servers can't reach the URL).
        # No certificate verification: these operator-configured feeds
        # routinely sit behind self-signed certificates.  A plain filesystem
        # path (e.g. a plot written by a run_script action) is read directly:
        # urlopen rejects it for lacking a scheme.
        try:
            if str(url).startswith("/"):
                with open(str(url), "rb") as fp:
                    payload = fp.read()
            else:
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                with urllib.request.urlopen(
                    str(url), timeout=30, context=context
                ) as response:
                    payload = response.read()
        except Exception:
            self.log.exception("could not fetch photo from %s", url)
            self.broadcast(f"Could not fetch photo from {url}\n{message}")
            return
        for chat_id in self._broadcast_ids:
            try:
                self._submit(
                    self._app.bot.send_photo(
                        chat_id=chat_id, photo=payload, caption=message or None
                    ),
                    timeout=60,
                )
            except Exception:
                self.log.exception("could not send photo to %s", chat_id)

    def ask(self, question: str, timeout: datetime.timedelta) -> str:
        """Ask the listeners a yes/no question with inline buttons; the first
        answer from an authorized chat wins.  Times out to "no"."""
        if not self._listen_ids or not self._thread.is_alive():
            return "no"

        pending = _PendingQuestion(question)
        self._pending[pending.token] = pending
        seconds = max(1.0, timeout.total_seconds())
        keyboard = telegram.InlineKeyboardMarkup(
            [
                [
                    telegram.InlineKeyboardButton(
                        "Yes", callback_data=f"ask:{pending.token}:yes"
                    ),
                    telegram.InlineKeyboardButton(
                        "No", callback_data=f"ask:{pending.token}:no"
                    ),
                ]
            ]
        )
        try:
            for chat_id in self._listen_ids:
                message = self._submit(
                    self._app.bot.send_message(
                        chat_id=chat_id,
                        text=f"[waiting {seconds:.0f}s] {question}",
                        reply_markup=keyboard,
                    )
                )
                pending.messages.append(message)
        except Exception:
            self.log.exception("could not send question")

        pending.event.wait(seconds)
        self._pending.pop(pending.token, None)

        if pending.answer is None:
            self._edit_pending(pending, f"{question} (timed out)")
            return "no"
        return pending.answer

    # ------------------------------------------------------------------
    # telegram handlers (run on the bot loop)
    # ------------------------------------------------------------------

    def _authorized(self, update: telegram.Update) -> bool:
        chat = update.effective_chat
        allowed = set(self._listen_ids) | set(self._broadcast_ids)
        if chat is not None and chat.id in allowed:
            return True
        self.log.warning(
            "ignoring message from unauthorized chat %s", chat.id if chat else "?"
        )
        return False

    def _make_command_handler(self, name: str):
        async def handler(update: telegram.Update, context) -> None:
            if not self._authorized(update) or self._commands is None:
                return
            reply = await asyncio.to_thread(
                self._commands.handle, name, list(context.args or [])
            )
            await self._send_reply(update.effective_chat.id, reply)

        return handler

    async def _send_reply(self, chat_id: int, reply: Reply) -> None:
        markup = None
        if reply.buttons:
            markup = telegram.InlineKeyboardMarkup(
                [
                    [telegram.InlineKeyboardButton(label, callback_data=f"cmd:{value}")]
                    for label, value in reply.buttons
                ]
            )
        await self._app.bot.send_message(
            chat_id=chat_id, text=reply.text, reply_markup=markup
        )

    async def _on_button(self, update: telegram.Update, context) -> None:
        query = update.callback_query
        await query.answer()
        if not self._authorized(update):
            return
        data = query.data or ""

        if data.startswith("cmd:"):
            if self._commands is None:
                return
            await query.edit_message_text("Working...")
            reply = await asyncio.to_thread(
                self._commands.handle_button, data[len("cmd:") :]
            )
            await query.edit_message_text(reply.text)
            return

        if data.startswith("ask:"):
            _, token, answer = data.split(":", 2)
            pending = self._pending.get(token)
            if pending is None:
                await query.edit_message_text("(question expired)")
                return
            user = update.effective_user
            who = (
                (user.username or user.first_name or "operator") if user else "operator"
            )
            pending.answer = answer
            pending.event.set()
            self._loop.create_task(
                self._edit_pending_async(
                    pending, f"{pending.question}\nAnswered {answer!r} by {who}"
                )
            )

    def _edit_pending(self, pending: "_PendingQuestion", text: str) -> None:
        try:
            self._submit(self._edit_pending_async(pending, text))
        except Exception:
            self.log.debug("could not edit question message", exc_info=True)

    async def _edit_pending_async(self, pending: "_PendingQuestion", text: str) -> None:
        for message in pending.messages:
            try:
                await self._app.bot.edit_message_text(
                    text=text, chat_id=message.chat_id, message_id=message.message_id
                )
            except Exception:
                pass


class _PendingQuestion:
    def __init__(self, question: str):
        self.token = uuid.uuid4().hex[:12]
        self.question = question
        self.event = threading.Event()
        self.answer: str | None = None
        self.messages: list[telegram.Message] = []
