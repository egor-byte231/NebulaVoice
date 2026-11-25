"""Owner-only video worker. No Telegram update polling."""
import asyncio
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('NebulaVideo')
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

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

OWNER = int(os.environ["OWNER_ID"])
TOKEN = os.environ["BOT_TOKEN"]
SECRET = os.environ["WORKER_SECRET"]
BASE = "https://nebulavoice-ai.maxvyn09.workers.dev"
async def request_job(session, action, data):
    async with session.post(BASE+"/video/"+action, json=data, headers={"Authorization":"Bearer "+SECRET}, timeout=ClientTimeout(total=15), allow_redirects=False) as r:
        if r.status >= 400:
            log.warning("Video queue %s: HTTP %s", action, r.status)
        r.raise_for_status()
        return await r.json()

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
        try:
            job = (await request_job(session, "claim", {})).get("job")
            if not job:
                await asyncio.sleep(5)
                continue
            log.info("Video job claimed: %s", job.get("id"))
            if job.get("user_id") == OWNER and job.get("chat_id") == OWNER and valid(job["url"]):
                try:
                    await process(session, job["url"], OWNER)
                    log.info("Video uploaded: %s", job["id"])
                except Exception as e:
                    log.warning("Video processing failed: %s", type(e).__name__)
                    try:
                        await notify(session, OWNER, "⚠️ Не вдалося завантажити відео.")
                    except Exception:
                        pass
            for attempt in range(5):
                try:
                    await request_job(session, "done", {"id":job["id"],"token":job["token"]})
                    break
                except Exception:
                    await asyncio.sleep(2**attempt)
        except Exception as e:
            log.warning("Video queue polling failed: %s", type(e).__name__)
            await asyncio.sleep(30)

async def lifecycle(app):
    async with ClientSession(timeout=ClientTimeout(total=120)) as session:
        log.info("NebulaVideo started: 5 polling slots, ffmpeg=%s", bool(shutil.which("ffmpeg")))
        tasks = [asyncio.create_task(worker(session)) for _ in range(5)]
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

async def health(request):
    return web.json_response({"ready": True, "ffmpeg": bool(shutil.which("ffmpeg"))})

if __name__ == "__main__":
    app = web.Application(client_max_size=4096)
    app.router.add_get("/health", health)
    app.cleanup_ctx.append(lifecycle)
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("SERVER_PORT", os.getenv("PORT", "8080"))), access_log=None)
