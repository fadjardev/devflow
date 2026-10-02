"""The Telegram front end. Thin: parse a command, call `Flow`, send the answer.

Only ALLOWED_CHAT_ID is served; every other chat is ignored without a reply,
so the bot gives a stranger nothing to probe.
"""

from __future__ import annotations

import asyncio
import re
import io
import logging
from collections import OrderedDict
from pathlib import Path

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import fmt, logs
from .config import Settings
from .pipeline import Asked, Flow, FlowError
from .tasks import ID_RE
from .vault import VaultError

log = logging.getLogger("devflow")

# Telegram renders in a proportional font, so no column alignment: one command per
# line, grouped under headings. HTML parse mode; arguments in italics.
HELP = """<b>devflow</b>
Claude plans and reviews, opencode codes, you approve.

<b>📝 Start</b>
/task <i>project request</i>: new task
/ask <i>project question</i>: ask about the code (<i>all</i> for every project). Reply to the answer to follow up.

<b>✅ Decide</b>
/approve <i>id</i>: approve the plan (gate 1) or the diff (gate 2)
/reject <i>id reason</i>: send it back with your reason
/answer <i>id answer</i>: answer the planner's question

<b>👀 Follow</b>
/status: active tasks
/show <i>id</i>: spec, last review, history
/diff <i>id</i>: the full diff as a file
/log: recent warnings and errors
/log <i>id</i>: one task's log lines

<b>🔧 Fix</b>
/retry <i>id</i>: resume a blocked task
/refresh <i>id</i>: catch up with the latest development branch
/stop <i>id</i>: cancel the task (the branch is kept)

<i>project</i> is the full name or any unique part of it, e.g. <i>asm</i>.
<i>id</i> looks like 261002-01."""

# The menu Telegram shows when you type "/".
MENU = [
    ("task", "New task: /task <project> <request>"),
    ("ask", "Ask about a project: /ask <project|all> <question>"),
    ("status", "Active tasks"),
    ("approve", "Approve the plan or the diff: /approve <id>"),
    ("reject", "Send it back: /reject <id> <reason>"),
    ("answer", "Answer the planner: /answer <id> <answer>"),
    ("show", "Spec, review, history: /show <id>"),
    ("diff", "Full diff as a file: /diff <id>"),
    ("log", "Recent errors, or /log <id>"),
    ("retry", "Resume a blocked task: /retry <id>"),
    ("refresh", "Catch up with development: /refresh <id>"),
    ("stop", "Cancel a task: /stop <id>"),
    ("help", "All commands"),
]


class TelegramNotifier:
    def __init__(self, app: Application, chat_id: int):
        self.app, self.chat_id = app, chat_id

    async def _send(self, what: str, call) -> None:
        """One retry on a network blip or flood limit. After that the message is logged
        and dropped, so a Telegram outage never fails the task it reports on."""
        for attempt in (1, 2):
            try:
                await call()
                return
            except RetryAfter as e:
                wait = float(getattr(e.retry_after, "total_seconds", lambda: e.retry_after)())
                log.warning("Telegram flood limit; retrying %s in %.0fs", what, wait)
                await asyncio.sleep(wait)
            except (TimedOut, NetworkError) as e:
                if attempt == 2:
                    log.error("Telegram send failed, %s dropped: %s", what, e)
                    return
                log.warning("Telegram send failed (%s); retrying %s", e, what)
                await asyncio.sleep(3)
        log.error("Telegram still rate-limited, %s dropped", what)

    async def text(self, message: str) -> None:
        for chunk in fmt.split(message):
            send = lambda text, mode: self.app.bot.send_message(self.chat_id, text, parse_mode=mode)  # noqa: E731
            await self._send(f"message {chunk[:60]!r}", lambda: _deliver(send, chunk))

    async def photo(self, path: Path, caption: str) -> None:
        async def send():
            with open(path, "rb") as f:
                await self.app.bot.send_photo(self.chat_id, f, caption=caption[:1000])
        await self._send(f"photo {path.name}", send)


def _flow(ctx: ContextTypes.DEFAULT_TYPE) -> Flow:
    return ctx.application.bot_data["flow"]


def _args(update: Update, n: int) -> list[str]:
    """Split the message into the command plus up to n arguments; the last keeps its newlines."""
    parts = (update.effective_message.text or "").split(None, n)
    return parts[1:]


async def _deliver(send, chunk: str):
    """Send one piece as HTML; if Telegram can't parse it, send the original text plainly."""
    try:
        return await send(fmt.to_html(chunk), ParseMode.HTML)
    except BadRequest as e:
        log.warning("Telegram rejected the formatting (%s); sending plain text", e)
        return await send(chunk, None)


async def _reply(update: Update, text: str) -> list:
    """Reply with Markdown-ish text (see fmt), split as needed. Returns the sent messages."""
    send = lambda t, mode: update.effective_message.reply_text(t, parse_mode=mode)  # noqa: E731
    return [await _deliver(send, chunk) for chunk in fmt.split(text)]


def _handler(fn):
    async def wrapped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        text = (update.effective_message.text or "") if update.effective_message else ""
        words = text.split()
        token = logs.task_id.set(words[1] if len(words) > 1 and ID_RE.fullmatch(words[1]) else "-")
        log.info("command %s", text[:200])
        try:
            await fn(update, ctx)
        except (FlowError, VaultError) as e:
            log.info("refused: %s", e)
            await _reply(update, f"✋ {e}")
        except Exception as e:
            log.exception("command failed: %s", text[:200])
            await _reply(update, f"💥 {type(e).__name__}: {e}\n/log for details")
        finally:
            logs.task_id.reset(token)
    return wrapped


@_handler
async def cmd_help(update: Update, ctx):
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)


@_handler
async def cmd_task(update: Update, ctx):
    args = _args(update, 2)
    if len(args) < 2:
        raise FlowError("usage: /task <project> <request text>")
    t = await _flow(ctx).new_task(args[0], args[1])
    await _reply(update, f"📝 {t.id} created for {t.project}. Planning…")


@_handler
async def cmd_status(update: Update, ctx):
    await _reply(update, _flow(ctx).status())


@_handler
async def cmd_show(update: Update, ctx):
    args = _args(update, 1)
    if not args:
        raise FlowError("usage: /show <id>")
    await _reply(update, _flow(ctx).show(args[0]))


@_handler
async def cmd_approve(update: Update, ctx):
    args = _args(update, 1)
    if not args:
        raise FlowError("usage: /approve <id>")
    await update.effective_chat.send_action(ChatAction.TYPING)
    await _reply(update, await _flow(ctx).approve(args[0]))


@_handler
async def cmd_reject(update: Update, ctx):
    args = _args(update, 2)
    if len(args) < 2:
        raise FlowError("usage: /reject <id> <reason>")
    await _reply(update, await _flow(ctx).reject(args[0], args[1]))


@_handler
async def cmd_diff(update: Update, ctx):
    args = _args(update, 1)
    if not args:
        raise FlowError("usage: /diff <id>")
    patch = _flow(ctx).diff(args[0])
    if not patch:
        raise FlowError("empty diff")
    await update.effective_message.reply_document(io.BytesIO(patch.encode("utf-8")), filename=f"{args[0]}.diff")


@_handler
async def cmd_retry(update: Update, ctx):
    args = _args(update, 1)
    if not args:
        raise FlowError("usage: /retry <id>")
    await _reply(update, await _flow(ctx).retry(args[0]))


@_handler
async def cmd_refresh(update: Update, ctx):
    args = _args(update, 1)
    if not args:
        raise FlowError("usage: /refresh <id>")
    await update.effective_chat.send_action(ChatAction.TYPING)
    await _reply(update, await _flow(ctx).refresh(args[0]))


@_handler
async def cmd_stop(update: Update, ctx):
    args = _args(update, 1)
    if not args:
        raise FlowError("usage: /stop <id>")
    await _reply(update, await _flow(ctx).stop(args[0]))


ASK_MEMORY = 200  # answers you can still reply to


async def _answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE, target: Asked, question: str) -> None:
    """Runs in the background: an answer can take minutes, and PTB handles updates one at a time."""
    try:
        text, follow = await _flow(ctx).ask(target, question)
        asked: OrderedDict = ctx.application.bot_data.setdefault("asks", OrderedDict())
        for sent in await _reply(update, text):
            asked[sent.message_id] = follow  # replying to any part of the answer follows up
        while len(asked) > ASK_MEMORY:
            asked.popitem(last=False)
    except (FlowError, VaultError) as e:
        await _reply(update, f"✋ {e}")
    except Exception as e:
        log.exception("ask failed: %s", question[:200])
        await _reply(update, f"💥 {type(e).__name__}: {str(e)[:300]}\n/log for details")


def _start_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE, target: Asked, question: str) -> None:
    jobs: set = ctx.application.bot_data.setdefault("ask_jobs", set())
    job = asyncio.create_task(_answer(update, ctx, target, question))
    jobs.add(job)  # hold a reference until it finishes, or it may be garbage-collected
    job.add_done_callback(jobs.discard)


@_handler
async def cmd_ask(update: Update, ctx):
    args = _args(update, 2)
    if len(args) < 2:
        raise FlowError("usage: /ask <project|all> <question>\ne.g. /ask asm where is the stock opname total computed?")
    try:  # an unknown project fails now, not minutes later
        target = _flow(ctx).ask_target(args[0])
    except VaultError as e:
        raise FlowError(f"{e}\n\nThe project comes first: /ask asm where is login handled?") from None
    await update.effective_chat.send_action(ChatAction.TYPING)
    await _reply(update, f"🤔 looking into {target.label}…")
    _start_answer(update, ctx, target, args[1])


@_handler
async def on_reply(update: Update, ctx):
    """A plain-text reply to an /ask answer continues that conversation."""
    replied = update.effective_message.reply_to_message
    follow = ctx.application.bot_data.get("asks", {}).get(replied.message_id) if replied else None
    if not follow:
        raise FlowError("To follow up, reply to an /ask answer. For anything else, use a command (/help).")
    await _reply(update, f"🤔 following up on {follow.label}…")
    _start_answer(update, ctx, follow, update.effective_message.text or "")


@_handler
async def cmd_log(update: Update, ctx):
    args = _args(update, 1)
    arg = args[0] if args else ""
    if ID_RE.fullmatch(arg):
        found = logs.tail(logs.path(), 30, task=arg) + logs.tail(logs.path("mcp"), 10, task=arg)
        title, empty = f"Log for {arg}", f"Nothing logged for {arg}."
    else:
        count = min(int(arg), 20) if arg.isdigit() else 5
        found = logs.tail(logs.path(errors=True), count) + logs.tail(logs.path("mcp", errors=True), 3)
        title, empty = "Recent warnings and errors", "✅ No warnings or errors logged."
    if not found:
        await _reply(update, empty)
        return
    # One code block per entry, newest last; a long traceback keeps its end, where the cause is.
    blocks = [f"```\n{_compact(e if len(e) <= 900 else '…' + e[-900:])}\n```" for e in found]
    await _reply(update, f"📜 **{title}**\n_runtime\\devflow.log, newest last_\n\n" + "\n\n".join(blocks))


_STAMP = re.compile(r"^\d{4}-(\d\d-\d\d \d\d:\d\d):\d\d,\d+ (\w+)\s+", re.M)


def _compact(entry: str) -> str:
    """`2026-10-02 15:06:10,800 WARNING  [-] ...` -> `10-02 15:06 WARNING [-] ...` (fits a phone)."""
    return _STAMP.sub(r"\1 \2 ", entry)


async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Errors outside command handlers: polling, the job queue, Telegram itself."""
    err = ctx.error
    if isinstance(err, (NetworkError, TimedOut)):  # transient; polling retries on its own
        log.warning("Telegram network error: %s", err)
        return
    log.error("unhandled error (update %s)", getattr(update, "update_id", "-"),
              exc_info=(type(err), err, err.__traceback__) if err else None)
    flow = ctx.application.bot_data.get("flow")
    if flow:
        await flow.notify(f"💥 devflow error: {type(err).__name__}: {str(err)[:300]}\n/log for details")


async def _ignored(update: Update, ctx):
    chat = update.effective_chat
    log.warning("ignored message from chat %s", chat.id if chat else "?")


def build(settings: Settings) -> Application:
    if not settings.telegram_token or not settings.allowed_chat_id:
        raise SystemExit("set TELEGRAM_TOKEN and ALLOWED_CHAT_ID in devflow/.env")

    async def on_start(app: Application) -> None:
        logs.watch_loop(asyncio.get_running_loop())
        flow = Flow(settings, TelegramNotifier(app, settings.allowed_chat_id))
        app.bot_data["flow"] = flow
        try:  # only in the allowed chat: a stranger who finds the bot sees no menu
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in MENU],
                                          scope=BotCommandScopeChat(settings.allowed_chat_id))
        except Exception:
            log.warning("could not set the command menu", exc_info=True)
        await flow.recover()
        await flow.n.text("🟢 devflow up. /help")

    async def on_stop(app: Application) -> None:
        flow = app.bot_data.get("flow")
        if flow:
            await flow.close()

    app = Application.builder().token(settings.telegram_token).post_init(on_start).post_shutdown(on_stop).build()
    me = filters.Chat(chat_id=settings.allowed_chat_id)
    for name, fn in [("start", cmd_help), ("help", cmd_help), ("task", cmd_task), ("status", cmd_status),
                     ("show", cmd_show), ("approve", cmd_approve), ("reject", cmd_reject), ("answer", cmd_reject),
                     ("diff", cmd_diff), ("retry", cmd_retry), ("refresh", cmd_refresh), ("stop", cmd_stop),
                     ("log", cmd_log), ("ask", cmd_ask)]:
        app.add_handler(CommandHandler(name, fn, filters=me))
    app.add_handler(MessageHandler(me & filters.REPLY & filters.TEXT & ~filters.COMMAND, on_reply))
    app.add_handler(MessageHandler(~me, _ignored))
    app.add_error_handler(_on_error)
    return app


def main(settings: Settings) -> None:
    logs.setup("devflow")
    # The MCP servers can't rotate their own logs; do it now, while none is running.
    for name in ("mcp", "mcp-errors"):
        logs.roll(name)
    log.info("devflow starting (max %d workers, base %s/%s)", settings.max_workers,
             settings.git_remote, settings.base_branch)
    build(settings).run_polling(allowed_updates=Update.ALL_TYPES)
    log.info("devflow stopped")
