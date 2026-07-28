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
import contextlib
import datetime
import ipaddress
import logging
import queue
import socket
import ssl
import threading
import urllib.parse
import urllib.request
import uuid

import telegram
import telegram.ext

from chimera_supervisor.operator import OperatorCommands, Reply, SupervisorPort

_COMMANDS = ("list", "run", "info", "lock", "unlock", "reload", "help")

#: bound on queued operator broadcasts (drop-oldest beyond this)
_OUTBOX_SIZE = 200


class TelegramNotifier:
    """Notifier + operator commands over Telegram."""

    def __init__(
        self,
        token: str,
        broadcast_ids: list[int],
        listen_ids: list[int],
        supervisor: SupervisorPort | None = None,
        log: logging.Logger | None = None,
        verify_ssl: bool = True,
    ):
        self.log = log or logging.getLogger(__name__)
        self._broadcast_ids = list(broadcast_ids)
        self._listen_ids = list(listen_ids)
        # when False, send_photo skips TLS verification for every https host,
        # not just private ones — for observatories whose public-IP webcams
        # still serve self-signed certificates
        self._verify_ssl = verify_ssl
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
        # catch-all last: anything not matched above (/start, plain text) lands
        # here so new users learn their id and how to request access.
        self._app.add_handler(
            telegram.ext.MessageHandler(telegram.ext.filters.ALL, self._on_message)
        )

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="telegram-bot", daemon=True
        )
        # broadcasts are queued and delivered off the engine thread, so a
        # slow or unreachable Telegram can never hold up the safety loop
        self._outbox: queue.Queue[str] = queue.Queue(maxsize=_OUTBOX_SIZE)
        self._stopping = threading.Event()
        self._sender = threading.Thread(
            target=self._drain_outbox, name="telegram-outbox", daemon=True
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()
        self._sender.start()

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
        self._stopping.set()
        if self._sender.is_alive():
            self._sender.join(timeout=5)
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
        """Queue a message for the operator chats and return immediately.

        NEVER blocks: this is called from the engine thread inside actions,
        and it used to wait up to 30 s per chat id.  With three chats and
        Telegram unreachable a single notify stalled the checklist cycle for
        90 s — a close-down list that broadcasts at every step could stall
        for minutes, during exactly the weather that triggered the close.
        ``ask()`` genuinely needs an answer and keeps its own blocking path.
        """
        try:
            self._outbox.put_nowait(str(message))
        except queue.Full:
            # drop the OLDEST: during an outage the newest state of the
            # observatory is the one worth delivering
            with contextlib.suppress(queue.Empty):
                dropped = self._outbox.get_nowait()
                self.log.warning("telegram outbox full; dropped %r", dropped[:80])
            with contextlib.suppress(queue.Full):
                self._outbox.put_nowait(str(message))

    def _drain_outbox(self) -> None:
        """Deliver queued broadcasts; runs on the bot thread, never the
        engine's."""
        while not self._stopping.is_set():
            try:
                message = self._outbox.get(timeout=1.0)
            except queue.Empty:
                continue
            for chat_id in self._broadcast_ids:
                try:
                    self._submit(
                        self._app.bot.send_message(chat_id=chat_id, text=message)
                    )
                except Exception:
                    self.log.exception("could not broadcast to %s", chat_id)

    def broadcast_photo(self, url: str, message: str = "") -> None:
        # observatory cameras live on the local network, so fetch the image
        # here and upload the bytes (Telegram's servers can't reach the URL).
        try:
            payload = self._fetch_photo(str(url))
        except FileNotFoundError:
            # a plot that has not been rendered yet is routine, not a fault:
            # `make_queue` sends last night's plan first, and on a night with
            # no history that file does not exist - send_photo raised, the
            # item's on_error aborted, and the plan was built but never
            # announced (2026-07-22). Fall back to the text.
            self.log.info("photo %s does not exist (yet); sending text only", url)
            if message:
                self.broadcast(message)
            return
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

    @staticmethod
    def _is_private_host(host: str) -> bool:
        """True for a host that resolves only to private/link-local/loopback
        addresses — the self-signed observatory feeds this bot exists for."""
        if not host:
            return True  # no host at all: a local path
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return False
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
        return bool(addresses) and all(
            address.is_private or address.is_loopback or address.is_link_local
            for address in addresses
        )

    def _fetch_photo(self, url: str) -> bytes:
        """Read a photo from disk or fetch it over HTTP(S).

        Local paths (absolute, relative or ``file://``) are read directly:
        urlopen rejects a bare path for lacking a scheme, which is what the
        locally rendered plots always are.
        """
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme in ("", "file"):
            path = urllib.request.url2pathname(parsed.path) if parsed.scheme else url
            with open(path, "rb") as fp:
                return fp.read()

        # Certificate verification is bypassed for hosts on the local network
        # (always) and for every host when verify_ssl is off (observatories
        # whose external self-signed feeds would otherwise fail). By default
        # only private hosts are trusted, so a send_photo pointed at a public
        # endpoint keeps its TLS authentication.
        insecure = not self._verify_ssl or self._is_private_host(
            parsed.hostname or ""
        )
        if parsed.scheme == "https" and insecure:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self.log.debug("photo host %s: TLS not verified", parsed.hostname)
        else:
            context = None
        with urllib.request.urlopen(url, timeout=30, context=context) as response:
            return response.read()

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

    async def _reply_unauthorized(self, update: telegram.Update) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        try:
            await self._app.bot.send_message(
                chat_id=chat.id,
                text=(
                    "This bot is restricted to authorized operators.\n"
                    f"Your Telegram ID is {chat.id}.\n"
                    "Send this ID to the observatory staff and wait for approval."
                ),
            )
        except Exception:
            self.log.debug(
                "could not answer unauthorized chat %s", chat.id, exc_info=True
            )

    async def _on_message(self, update: telegram.Update, context) -> None:
        if not self._authorized(update):
            await self._reply_unauthorized(update)

    def _make_command_handler(self, name: str):
        async def handler(update: telegram.Update, context) -> None:
            if not self._authorized(update):
                await self._reply_unauthorized(update)
                return
            if self._commands is None:
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
