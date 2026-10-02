"""The Telegram front end. Thin: parse a command, call `Flow`, send the answer.

Only ALLOWED_CHAT_ID is served; every other chat is ignored without a reply,
so the bot gives a stranger nothing to probe.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections import OrderedDict
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import logs
from .config import Settings
from .pipeline import Asked, Flow, FlowError
from .tasks import ID_RE
from .vault import VaultError

log = logging.getLogger("devflow")
LIMIT = 4000  # Telegram caps a message at 4096 characters

HELP = """devflow — Claude plans and reviews, opencode codes, you approve.

/task <project> <request>   new task (project: name or unique part, e.g. asm)
/ask <project|all> <question>  ask about the code; reply to the answer to follow up
/status                     active tasks
/show <id>                  spec, last review, history
/approve <id>               pass gate 1 (plan) or gate 2 (diff)
/reject <id> <why>          send the plan back to the planner, or the diff back to the worker
/answer <id> <answer>       answer a planner's question (same as /reject)
/diff <id>                  the full diff as a file
/retry <id>                 resume a blocked task
/refresh <id>               update a reviewed task onto the latest shared branch
/stop <id>                  cancel; the branch is kept
/log [<id>|<n>]             last n warnings/errors (default 5), or one task's log lines
"""


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
        for i in range(0, len(message), LIMIT):
            chunk = message[i:i + LIMIT]
            await self._send(f"message {chunk[:60]!r}", lambda: self.app.bot.send_message(self.chat_id, chunk))

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


async def _reply(update: Update, text: str) -> None:
    for i in range(0, len(text), LIMIT):
        await update.effective_message.reply_text(text[i:i + LIMIT])


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
    await _reply(update, HELP)


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
        for i in range(0, len(text), LIMIT):
            sent = await update.effective_message.reply_text(text[i:i + LIMIT])
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
    target = _flow(ctx).ask_target(args[0])  # an unknown project fails now, not minutes later
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
        title = f"log lines for {arg}"
    else:
        count = min(int(arg), 20) if arg.isdigit() else 5
        found = logs.tail(logs.path(errors=True), count) + logs.tail(logs.path("mcp", errors=True), 3)
        title = "last warnings/errors"
    if not found:
        await _reply(update, f"No {title}.")
        return
    # Newest last, and each traceback trimmed to its end, where the cause is.
    body = "\n\n".join(e if len(e) <= 900 else "…" + e[-900:] for e in found)
    await _reply(update, f"📜 {title} (runtime/devflow.log)\n\n{body[-(LIMIT * 2):]}")


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
