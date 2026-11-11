import asyncio
import json
import logging
import math
import os
import re
import tempfile
from pathlib import Path

import aiohttp

from payments import CLIENT_STAGES, PLANS, Payments

SIZE = 640
MAX_SECONDS = 60
MAX_NOTE_BYTES = 12 * 1024**2
VIDEO_IO_TIMEOUT = 600
VIDEO_FORMATS = "mov,matroska,webm,avi,mpegts,mpeg,flv,ogg,asf"
WELCOME = "👋 Привіт! Я перетворюю відео на кружечки в Telegram. Інструкція — /help."
TARIFFS = (
    "🎁 Спробуй безкоштовно\n"
    "1 кружечок · до 200 МБ · до 60 секунд\n\n"
    "☀️ RP День — 46 грн\n"
    "Доступ на 1 день\n"
    "До 200 МБ · до 60 секунд · 1 кружечок з відео\n\n"
    "✨ RP П’ять днів — 230 грн\n"
    "Доступ на 5 днів\n"
    "До 250 МБ · до 60 секунд · 1 кружечок з відео\n\n"
    "🚀 RP Тиждень — 391 грн\n"
    "Доступ на 7 днів\n"
    "До 300 МБ · до 2 хвилин\n"
    "Автонарізка на 2 кружечки, якщо відео довше 60 секунд\n\n"
    "👑 RP Місяць — 414 грн\n"
    "Доступ на 30 днів\n"
    "До 400 МБ · до 3 хвилин\n"
    "Автонарізка на частини до 60 секунд — максимум 3 кружечки"
)
HELP = (
    "🎥 Як зробити відеокружечок\n\n"
    "Надішли сюди відео — отримаєш кругле відеоповідомлення зі звуком.\n\n"
    "📎 Для кращої якості надсилай оригінал як файл. Найкраще підійде MP4.\n\n"
    "🎯 Тримай головне в центрі кадру — бот обрізає відео по центру.\n\n"
    "✂️ Довгі відео ділю на частини до 60 секунд відповідно до тарифу.\n\n"
    + TARIFFS
)


class VideoError(Exception):
    pass


class TelegramError(Exception):
    def __init__(self, code, retry_after=0, description=""):
        self.code = code
        self.retry_after = retry_after
        description = re.sub(r"https?://\S+", "[url]", str(description))
        description = re.sub(r"\d{6,}:[A-Za-z0-9_-]+", "[token]", description)
        self.description = " ".join(description.split())[:400]
        super().__init__(self.description or str(code))


async def media_command(*args, timeout):
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    if process.returncode:
        logging.warning("%s failed (exit=%s)", args[0], process.returncode)
        raise VideoError("Не вдалося прочитати відео. Спробуй надіслати MP4.")
    return output


async def video_duration(source):
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
    return duration


async def convert(source, output, start=0, duration=MAX_SECONDS):
    args = [
        "ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file",
        "-format_whitelist", VIDEO_FORMATS,
        "-threads", "2", "-ss", str(start), "-i", str(source), "-map", "0:V:0", "-map", "0:a:0?",
        "-vf", "scale=trunc(iw*sar/2)*2:ih,setsar=1,"
        f"crop='min(iw,ih)':'min(iw,ih)',scale={SIZE}:{SIZE}:flags=lanczos",
        "-filter_threads", "2", "-fpsmax", "60", "-t", str(duration),
        "-c:v", "libx264", "-threads", "2", "-preset", "medium",
        "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-ac", "2", "-map_metadata", "-1", "-movflags", "+faststart",
    ]
    await media_command(*args, str(output), timeout=600)
    if output.stat().st_size > MAX_NOTE_BYTES:
        # Reserve 10% for container overhead and bitrate variation.
        video_bitrate = max(64000, int(MAX_NOTE_BYTES * 8 * 0.90 / duration) - 192000)
        rate_index = args.index("-crf")
        args[rate_index:rate_index + 2] = ["-b:v", str(video_bitrate)]
        pass_log = str(output.with_suffix(".pass"))
        await media_command(
            *args, "-pass", "1", "-passlogfile", pass_log,
            "-an", "-f", "null", "-y", os.devnull, timeout=600,
        )
        await media_command(
            *args, "-pass", "2", "-passlogfile", pass_log,
            "-y", str(output), timeout=600,
        )
        if output.stat().st_size > MAX_NOTE_BYTES:
            raise VideoError("Кружечок перевищив ліміт Telegram. Спробуй коротший фрагмент.")
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
        options = {}
        if method in {"getFile", "sendVideoNote"}:
            options["timeout"] = aiohttp.ClientTimeout(total=VIDEO_IO_TIMEOUT, connect=10)
        elif method == "sendChatAction":
            options["timeout"] = aiohttp.ClientTimeout(total=5)
        async with self.session.post(
            f"{self.base}/{method}", json=data if form is None else None, data=form, **options
        ) as response:
            try:
                result = await response.json()
            except (ValueError, aiohttp.ContentTypeError):
                raise TelegramError(response.status) from None
            if not result.get("ok"):
                raise TelegramError(result.get("error_code", response.status),
                                    result.get("parameters", {}).get("retry_after", 0),
                                    result.get("description", ""))
            return result["result"]

    def file_limit(self, user_id):
        megabytes, _ = self.payments.limits(user_id)
        return min(megabytes * 1024**2, 500 * 1024**2 if self.local_api else self.max_bytes)

    async def download(self, file_path, destination, limit=None):
        limit = self.max_bytes if limit is None else limit
        if Path(file_path).is_absolute():
            source = Path(file_path).resolve()
            if not self.local_api or not source.is_relative_to(self.cache_dir.resolve()):
                raise VideoError("Бот не зміг відкрити файл. Спробуй ще раз.")
            if source.stat().st_size > limit:
                raise VideoError("Файл завеликий.")
            return source

        async with self.session.get(
            f"{self.file_base}/{file_path}",
            timeout=aiohttp.ClientTimeout(total=VIDEO_IO_TIMEOUT, connect=10),
        ) as response:
            if response.status != 200:
                raise TelegramError(response.status)
            size = 0
            with destination.open("wb") as stream:
                async for chunk in response.content.iter_chunked(256 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise VideoError("Файл завеликий.")
                    stream.write(chunk)
        return destination

    async def notify(self, chat_id, text):
        await self.call("sendMessage", chat_id=chat_id, text=text)

    async def show_access(self, chat_id, text=""):
        buttons = [[{"text": "💎 Переглянути тарифи", "callback_data": "plans"}]]
        megabytes, seconds = self.payments.limits(chat_id)
        duration_text = f"{seconds} с" if seconds is not None else "автонарізка довгих відео"
        current_limits = f"\n📎 Твій ліміт — {self.file_limit(chat_id) // 1024**2} МБ · {duration_text}."
        await self.call(
            "sendMessage", chat_id=chat_id, text=text + self.payments.status(chat_id) + current_limits,
            reply_markup={"inline_keyboard": buttons},
        )

    async def show_plans(self, chat_id):
        buttons = []
        if self.payments.enabled and chat_id != self.payments.owner:
            buttons = [
                [{"text": f"{title} · {amount} грн", "callback_data": f"buy:{plan}"}]
                for plan, (title, _, amount) in PLANS.items()
            ]
        buttons.append([{"text": "📋 Мій доступ", "callback_data": "access"}])
        await self.call("sendMessage", chat_id=chat_id,
                        text="💎 Обери свій тариф\n\n" + TARIFFS,
                        reply_markup={"inline_keyboard": buttons})

    async def show_client_report(self, chat_id, command, args):
        if chat_id != self.payments.owner:
            return
        if not self.payments.enabled:
            await self.notify(chat_id, "База клієнтів доступна після підключення платежів і сховища /data.")
            return

        if command == "/stats":
            stages, plans = self.payments.client_report()
            lines = [f"📊 Клієнтів у базі: {sum(stages.values())}", ""]
            for key, title in CLIENT_STAGES.items():
                lines.append(f"{title}: {stages.get(key, 0)}")
            lines.extend(["", "Підтверджені покупки за тарифами"])
            purchases = {row["plan"]: row for row in plans}
            for key in dict.fromkeys([*PLANS, *purchases]):
                row = purchases.get(key)
                title = PLANS[key][0] if key in PLANS else key
                lines.append(f"{title}: {row['purchases'] if row else 0}")
            lines.extend([f"Сума підтверджених оплат: {sum(row['revenue'] for row in plans)} грн",
                          "", "/clients — переглянути список за стадією"])
            await self.notify(chat_id, "\n".join(lines))
            return

        parts = args.split()
        stage, page_text = (parts + ["1"])[:2] if parts else ("", "1")
        if stage not in CLIENT_STAGES or len(parts) > 2 or not re.fullmatch(r"[1-9][0-9]{0,5}", page_text):
            lines = ["Вибери стадію клієнтів:"] + [
                f"/clients {key} — {title}" for key, title in CLIENT_STAGES.items()
            ]
            await self.notify(chat_id, "\n".join(lines))
            return
        page = int(page_text)
        rows = self.payments.client_report(stage, (page - 1) * 10)
        lines = [f"{CLIENT_STAGES[stage]} · сторінка {page}", ""]
        if not rows:
            lines.append("На цій сторінці клієнтів немає.")
        for row in rows[:10]:
            name = " ".join((row["first_name"] or "").split())[:80]
            username = f" @{row['username']}" if row["username"] else ""
            plan = row["paid_plan"] or row["pending_plan"]
            title = PLANS[plan][0] if plan in PLANS else plan
            label = "Остання покупка" if row["paid_plan"] else "Рахунок"
            detail = f" · {label}: {title}" if title else ""
            lines.append(f"{row['user_id']} · {name or 'Ім’я відсутнє'}{username}{detail}")
        if len(rows) > 10:
            lines.extend(["", f"Далі → /clients {stage} {page + 1}"])
        await self.notify(chat_id, "\n".join(lines))

    async def handle_callback(self, callback):
        await self.call("answerCallbackQuery", callback_query_id=callback["id"])
        user_id = callback["from"]["id"]
        chat = callback.get("message", {}).get("chat", {})
        data = callback.get("data", "")
        if chat.get("type") != "private" or chat.get("id") != user_id:
            return
        if self.allowed_users and user_id not in self.allowed_users and user_id != self.payments.owner:
            return
        self.payments.remember_client(callback["from"])
        if data == "plans":
            await self.show_plans(user_id)
            return
        if data == "access":
            await self.show_access(user_id)
            return
        if not self.payments.enabled or user_id == self.payments.owner:
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

        text, _, args = (message.get("text") or "").strip().partition(" ")
        command = text.split("@")[0]
        if command in {"/stats", "/clients"}:
            await self.show_client_report(chat_id, command, args)
            return

        self.payments.remember_client(message.get("from") or chat)
        media = message.get("video") or message.get("document")
        if not media:
            if command == "/paysupport":
                await self.notify(chat_id, "Питання щодо оплати — @P2P_Marshal. Вкажи номер рахунку, якщо він є.")
            elif command == "/plans":
                await self.show_plans(chat_id)
            elif command == "/help":
                await self.notify(chat_id, HELP)
            else:
                intro = "" if command == "/access" else WELCOME + "\n\n"
                await self.show_access(chat_id, intro)
            return

        if self.payments.blocked(chat_id):
            await self.show_access(chat_id, "Безкоштовний кружечок уже використано. Обери тариф.\n\n")
            return
        trial = self.payments.use_trial(chat_id)
        stage = "validation"
        delivered = 0
        limit = self.file_limit(chat_id)
        _, max_duration = self.payments.limits(chat_id)
        try:
            if (media.get("file_size") or 0) > limit:
                raise VideoError(f"Файл завеликий. Твій ліміт — {limit // 1024**2} МБ.")
            if max_duration is not None and (media.get("duration") or 0) > max_duration:
                raise VideoError(f"Ліміт твого доступу — {max_duration} секунд на відео. Тарифи — /plans.")
            stage = "getFile"
            try:
                await self.call("sendChatAction", chat_id=chat_id, action="upload_video_note")
            except (TelegramError, aiohttp.ClientError, TimeoutError) as error:
                logging.warning("Video progress indicator failed (%s)", type(error).__name__)
            file = await self.call("getFile", file_id=media["file_id"])
            with tempfile.TemporaryDirectory(prefix="rp-video-") as folder:
                stage = "download"
                source = await self.download(file["file_path"], Path(folder) / "input", limit)
                stage = "probe"
                total_duration = await video_duration(source)
                if max_duration is not None and total_duration > max_duration:
                    raise VideoError(f"Ліміт твого доступу — {max_duration} секунд на відео. Тарифи — /plans.")
                parts = math.ceil(total_duration / MAX_SECONDS)
                if trial and parts > 1:
                    raise VideoError("Безкоштовно доступний один кружечок. Надішли відео до 60 секунд або обери тариф /plans.")
                await self.notify(chat_id, f"🎬 Обробляю відео. Частин: {parts}. Зачекай, будь ласка.")
                for index in range(parts):
                    output = Path(folder) / "circle.mp4"
                    stage = "convert"
                    seconds = min(MAX_SECONDS, total_duration - index * MAX_SECONDS)
                    duration = await convert(source, output, index * MAX_SECONDS, seconds)
                    stage = "sendVideoNote"
                    with output.open("rb") as video:
                        form = aiohttp.FormData()
                        form.add_field("chat_id", str(chat_id))
                        form.add_field("length", str(SIZE))
                        form.add_field("duration", str(min(MAX_SECONDS, duration)))
                        form.add_field("reply_parameters", json.dumps({
                            "message_id": message["message_id"],
                            "allow_sending_without_reply": True,
                        }))
                        form.add_field("video_note", video, filename=output.name, content_type="video/mp4")
                        await self.call("sendVideoNote", form=form)
                    delivered += 1
                    if trial:
                        self.payments.record_trial(chat_id, message["message_id"])
                    output.unlink()
                logging.info("Video completed parts=%s", delivered)
        except VideoError as error:
            await self.notify(chat_id, f"Надіслано частин: {delivered}. " + str(error))
        except (TelegramError, aiohttp.ClientError, TimeoutError, OSError, KeyError, ValueError) as error:
            logging.warning(
                "Video failed: stage=%s type=%s code=%s size=%s duration=%s reason=%s",
                stage, type(error).__name__, getattr(error, "code", getattr(error, "errno", None)),
                media.get("file_size"), media.get("duration"), getattr(error, "description", ""),
            )
            hints = {
                "getFile": "Не вдалося отримати відео з Telegram. Спробуй надіслати його як файл.",
                "download": "Не вдалося завантажити відео. Спробуй надіслати його як файл.",
                "convert": "Не вдалося обробити відео. Спробуй коротший ролик у форматі MP4.",
                "sendVideoNote": "Кружечок створено, але Telegram не підтвердив надсилання. Перевір чат і за потреби повтори.",
            }
            detail = ""
            if chat_id == self.payments.owner and isinstance(error, TelegramError):
                detail = f"\nTelegram {error.code} — {error.description or 'опис відсутній'}"
            await self.notify(chat_id, f"Надіслано частин: {delivered}. " + hints.get(stage, "Не вдалося обробити відео. Спробуй MP4.") + f" Код етапу: {stage}." + detail)

    async def run(self):
        offset = 0
        configured = False
        while True:
            try:
                if not configured:
                    await self.call("setMyCommands", commands=[
                        {"command": "start", "description": "Почати"},
                        {"command": "help", "description": "Як зробити відеокружечок"},
                        {"command": "access", "description": "Мій доступ"},
                        {"command": "plans", "description": "Тарифи"},
                        {"command": "paysupport", "description": "Підтримка щодо оплати"},
                    ])
                    await self.call("setMyDescription", description=WELCOME)
                    logging.info("Версія 1_8. Ліміт %s МБ, OWNER %s МБ. Автонарізка увімкнена", self.max_bytes // 1024**2, self.file_limit(self.payments.owner) // 1024**2)
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
