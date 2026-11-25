"""Owner-only video worker. No Telegram update polling."""
import asyncio
import hmac
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from aiohttp import web, ClientSession, ClientTimeout, FormData

HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "instagram.com", "www.instagram.com"}
def valid(url):
    try:
        p = urlsplit(url)
        return p.scheme == "https" and p.hostname in HOSTS and not p.username and not p.password and p.port in (None, 443)
    except ValueError:
        return False

OWNER = int(os.environ["OWNER_ID"])
TOKEN = os.environ["BOT_TOKEN"]
SECRET = os.environ["VIDEO_WORKER_SECRET"]
if len(SECRET) < 32:
    raise RuntimeError("VIDEO_WORKER_SECRET must have at least 32 characters")
queue = asyncio.Queue(maxsize=15)

async def notify(session, chat, text):
    async with session.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": chat, "text": text}) as r:
        await r.read()

async def process(session, url, chat):
    with tempfile.TemporaryDirectory(prefix="video-", dir=os.getcwd()) as tmp:
        ffmpeg = shutil.which("ffmpeg")
        fmt = "b[ext=mp4][height<=720]/b[ext=mp4]" if not ffmpeg else "bv*[ext=mp4][height<=720]+ba[ext=m4a]/b[ext=mp4][height<=720]/b[ext=mp4]"
        proc = await asyncio.create_subprocess_exec("python", "-m", "yt_dlp", "--no-playlist", "--no-cache-dir", "--no-progress", "--max-filesize", "45M", "--socket-timeout", "20", "--retries", "1", "-f", fmt, "--merge-output-format", "mp4", "-o", str(Path(tmp)/"video.%(ext)s"), "--", url, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), 120)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        path = Path(tmp)/"video.mp4"
        if proc.returncode or not path.is_file() or path.stat().st_size > 49_000_000:
            raise RuntimeError("download failed or exceeds limit")
        with path.open("rb") as f:
            data = FormData()
            data.add_field("chat_id", str(chat))
            data.add_field("video", f, filename="video.mp4", content_type="video/mp4")
            async with session.post(f"https://api.telegram.org/bot{TOKEN}/sendVideo", data=data) as r:
                if r.status != 200:
                    raise RuntimeError("Telegram upload failed")

async def worker(session):
    while True:
        url, chat = await queue.get()
        try:
            await process(session, url, chat)
        except Exception:
            try:
                await notify(session, chat, "⚠️ Не вдалося завантажити відео. Можливе обмеження платформи, розміру або формату.")
            except Exception:
                pass
        finally:
            queue.task_done()

async def submit(request):
    if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer "+SECRET):
        raise web.HTTPUnauthorized()
    data = await request.json()
    if not isinstance(data, dict) or data.get("user_id") != OWNER or data.get("chat_id") != OWNER:
        raise web.HTTPForbidden()
    url = data.get("url")
    if not isinstance(url, str) or len(url)>2048 or not valid(url):
        raise web.HTTPBadRequest()
    try:
        queue.put_nowait((url, OWNER))
    except asyncio.QueueFull:
        raise web.HTTPServiceUnavailable()
    return web.json_response({"accepted": True}, status=202)

async def lifecycle(app):
    async with ClientSession(timeout=ClientTimeout(total=120)) as session:
        tasks = [asyncio.create_task(worker(session)) for _ in range(5)]
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

async def health(request):
    return web.json_response({"ready": True, "ffmpeg": bool(shutil.which("ffmpeg"))})

if __name__ == "__main__":
    app = web.Application(client_max_size=4096)
    app.router.add_post("/jobs", submit)
    app.router.add_get("/health", health)
    app.cleanup_ctx.append(lifecycle)
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("SERVER_PORT", os.getenv("PORT", "8080"))), access_log=None)
