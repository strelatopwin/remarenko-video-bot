import asyncio
import json
import logging
import math
import os
import tempfile
from pathlib import Path

import aiohttp

from payments import PLANS, Payments

SIZE = 640
MAX_SECONDS = 60
VIDEO_FORMATS = "mov,matroska,webm,avi,mpegts,mpeg,flv,ogg,asf"
HELP = (
    "🎥 Надішли відео до 60 секунд, і я поверну його кружечком. "
    "Можна надіслати відео або прикріпити його як файл."
)


class VideoError(Exception):
    pass


class TelegramError(Exception):
    def __init__(self, code, retry_after=0):
        self.code = code
        self.retry_after = retry_after


async def media_command(*args, timeout):
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        raise VideoError("Не вдалося прочитати відео. Спробуй надіслати MP4.")
    return output


async def convert(source, output):
    metadata = json.loads(await media_command(
        "ffprobe", "-v", "error", "-protocol_whitelist", "file",
        "-format_whitelist", VIDEO_FORMATS,
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "json", str(source), timeout=20,
    ))
    if not any(stream["codec_type"] == "video" for stream in metadata.get("streams", [])):
        raise VideoError("У файлі немає відео. Надішли відеофайл.")
    duration = float(metadata.get("format", {}).get("duration", "nan"))
    if not math.isfinite(duration) or duration <= 0:
        raise VideoError("Не вдалося визначити тривалість. Спробуй надіслати MP4.")
    if duration > MAX_SECONDS:
        raise VideoError("Відео довше 60 секунд. Обріж його в Telegram і надішли ще раз.")

    await media_command(
        "ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file",
        "-format_whitelist", VIDEO_FORMATS,
        "-threads", "2", "-i", str(source), "-map", "0:V:0", "-map", "0:a:0?",
        "-vf", "scale=trunc(iw*sar/2)*2:ih,setsar=1,"
        f"crop='min(iw,ih)':'min(iw,ih)',scale={SIZE}:{SIZE}:flags=lanczos",
        "-filter_threads", "2", "-fpsmax", "60", "-t", str(MAX_SECONDS),
        "-c:v", "libx264", "-threads", "2", "-preset", "medium",
        "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-ac", "2", "-map_metadata", "-1", "-movflags", "+faststart",
        str(output), timeout=120,
    )
    return max(1, round(duration))


class Bot:
    def __init__(self, session):
        self.session = session
        self.payments = Payments(session)
        token = os.environ["BOT_TOKEN"]
        self.base = f'{os.environ.get("BOT_API_URL", "https://api.telegram.org").rstrip("/")}/bot{token}'
        self.file_base = self.base.replace("/bot" + token, "/file/bot" + token)
        self.local_api = "BOT_API_URL" in os.environ
        self.cache_dir = Path(os.environ.get("TELEGRAM_CACHE_DIR", "/var/lib/telegram-bot-api"))
        self.max_bytes = (200 if self.local_api else 20) * 1024 * 1024
        self.allowed_users = {
            int(user_id) for user_id in os.environ.get("ALLOWED_USER_IDS", "").split(",")
            if user_id.strip()
        }

    async def call(self, method, *, form=None, **data):
        async with self.session.post(f"{self.base}/{method}", json=data if form is None else None, data=form) as response:
            try:
                result = await response.json()
            except (ValueError, aiohttp.ContentTypeError):
                raise TelegramError(response.status) from None
            if not result.get("ok"):
                raise TelegramError(result.get("error_code", response.status),
                                    result.get("parameters", {}).get("retry_after", 0))
            return result["result"]

    async def download(self, file_path, destination):
        if Path(file_path).is_absolute():
            source = Path(file_path).resolve()
            if not self.local_api or not source.is_relative_to(self.cache_dir.resolve()):
                raise VideoError("Бот не зміг відкрити файл. Спробуй ще раз.")
            if source.stat().st_size > self.max_bytes:
                raise VideoError("Файл завеликий.")
            return source

        async with self.session.get(f"{self.file_base}/{file_path}") as response:
            if response.status != 200:
                raise TelegramError(response.status)
            size = 0
            with destination.open("wb") as stream:
                async for chunk in response.content.iter_chunked(256 * 1024):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise VideoError("Файл завеликий.")
                    stream.write(chunk)
        return destination

    async def notify(self, chat_id, text):
        await self.call("sendMessage", chat_id=chat_id, text=text)

    async def show_access(self, chat_id, text=""):
        buttons = []
        if self.payments.enabled and chat_id != self.payments.owner:
            buttons = [
                [{"text": f"{title} · {amount} грн", "callback_data": f"buy:{plan}"}]
                for plan, (title, _, amount) in PLANS.items()
            ]
            buttons.append([{"text": "Мій доступ", "callback_data": "access"}])
        await self.call(
            "sendMessage", chat_id=chat_id, text=text + self.payments.status(chat_id),
            reply_markup={"inline_keyboard": buttons},
        )

    async def handle_callback(self, callback):
        await self.call("answerCallbackQuery", callback_query_id=callback["id"])
        user_id = callback["from"]["id"]
        chat = callback.get("message", {}).get("chat", {})
        data = callback.get("data", "")
        if chat.get("type") != "private" or chat.get("id") != user_id:
            return
        if (not self.payments.enabled or user_id == self.payments.owner
                or self.allowed_users and user_id not in self.allowed_users):
            return
        if data == "access":
            await self.notify(user_id, self.payments.status(user_id))
            return
        if not data.startswith("buy:") or data[4:] not in PLANS:
            return
        plan = data[4:]
        try:
            url = await self.payments.invoice(user_id, plan)
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            logging.warning("WayForPay invoice failed (%s)", type(error).__name__)
            await self.notify(user_id, "Не вдалося створити рахунок. Спробуй пізніше або напиши @P2P_Marshal.")
            return
        title, _, amount = PLANS[plan]
        await self.call(
            "sendMessage", chat_id=user_id,
            text=f"{title} · {amount} грн. Доступ активується після підтвердження оплати.",
            reply_markup={"inline_keyboard": [[{"text": "Оплатити через WayForPay", "url": url}]]},
        )

    async def handle(self, message):
        chat = message["chat"]
        if chat["type"] != "private":
            return
        chat_id = chat["id"]
        if chat_id != self.payments.owner and self.allowed_users and chat_id not in self.allowed_users:
            await self.call("sendMessage", chat_id=chat_id, text="Цей бот має обмежений доступ.")
            return

        media = message.get("video") or message.get("document")
        if not media:
            command = (message.get("text") or "").partition(" ")[0].split("@")[0]
            if command == "/paysupport":
                await self.notify(chat_id, "Питання щодо оплати — @P2P_Marshal. Вкажи номер рахунку, якщо він є.")
            else:
                await self.show_access(chat_id, "" if command in {"/access", "/plans"} else HELP + "\n\n")
            return

        if self.payments.blocked(chat_id):
            await self.show_access(chat_id, "Пробні конвертації закінчилися. Обери тариф.\n\n")
            return
        trial = self.payments.use_trial(chat_id)
        try:
            if (media.get("file_size") or 0) > self.max_bytes:
                raise VideoError(f"Файл завеликий. Ліміт цього бота — {self.max_bytes // 1024**2} МБ.")
            if (media.get("duration") or 0) > MAX_SECONDS:
                raise VideoError("Відео довше 60 секунд. Обріж його в Telegram і надішли ще раз.")
            await self.call("sendChatAction", chat_id=chat_id, action="upload_video_note")
            file = await self.call("getFile", file_id=media["file_id"])
            with tempfile.TemporaryDirectory(prefix="rp-video-") as folder:
                output = Path(folder) / "circle.mp4"
                source = await self.download(file["file_path"], Path(folder) / "input")
                duration = await convert(source, output)
                with output.open("rb") as video:
                    form = aiohttp.FormData()
                    form.add_field("chat_id", str(chat_id))
                    form.add_field("length", str(SIZE))
                    form.add_field("duration", str(duration))
                    form.add_field("reply_parameters", json.dumps({
                        "message_id": message["message_id"],
                        "allow_sending_without_reply": True,
                    }))
                    form.add_field("video_note", video, filename=output.name, content_type="video/mp4")
                    await self.call("sendVideoNote", form=form)
                if trial:
                    self.payments.record_trial(chat_id, message["message_id"])
        except VideoError as error:
            await self.call("sendMessage", chat_id=chat_id, text=str(error))
        except (TelegramError, aiohttp.ClientError, TimeoutError, OSError, KeyError, ValueError) as error:
            logging.warning("Conversion request failed (%s)", type(error).__name__)
            await self.call("sendMessage", chat_id=chat_id,
                            text="Не вдалося зробити кружечок. Спробуй надіслати відео ще раз.")

    async def run(self):
        offset = 0
        configured = False
        while True:
            try:
                if not configured:
                    await self.call("setMyCommands", commands=[
                        {"command": "start", "description": "Як зробити відеокружечок"},
                        {"command": "help", "description": "Підказка щодо відео"},
                        {"command": "access", "description": "Мій доступ"},
                        {"command": "plans", "description": "Тарифи"},
                        {"command": "paysupport", "description": "Підтримка щодо оплати"},
                    ])
                    await self.call("setMyDescription", description=HELP)
                    logging.info("Бот готовий приймати відео до %s МБ", self.max_bytes // 1024**2)
                    configured = True
                updates = await self.call("getUpdates", offset=offset, timeout=25,
                                          allowed_updates=["message", "callback_query"])
            except TelegramError as error:
                logging.warning("Telegram API returned %s", error.code)
                await asyncio.sleep(max(error.retry_after, 5))
                continue
            except (aiohttp.ClientError, TimeoutError):
                logging.warning("Telegram API connection failed")
                await asyncio.sleep(5)
                continue
            for update in updates:
                try:
                    if "message" in update:
                        await self.handle(update["message"])
                    elif "callback_query" in update:
                        await self.handle_callback(update["callback_query"])
                except (TelegramError, aiohttp.ClientError, TimeoutError) as error:
                    logging.warning("Could not answer update (%s)", type(error).__name__)
                offset = update["update_id"] + 1


async def main():
    logging.basicConfig(level=logging.INFO)
    if not os.environ.get("BOT_TOKEN"):
        raise SystemExit("Set BOT_TOKEN before starting the bot.")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        bot = Bot(session)
        if bot.payments.enabled:
            async with asyncio.TaskGroup() as group:
                group.create_task(bot.payments.run(bot.notify))
                group.create_task(bot.run())
        else:
            await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
