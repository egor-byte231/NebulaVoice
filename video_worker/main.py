"""Public video worker. No Telegram update polling."""
import asyncio
import time
import sys
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('NebulaVideo')
import os
import json
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from aiohttp import web, ClientSession, ClientTimeout, FormData, TCPConnector

HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "instagram.com", "www.instagram.com","facebook.com","www.facebook.com","m.facebook.com","fb.watch","fb.com","x.com","www.x.com","twitter.com","www.twitter.com","mobile.twitter.com","t.co","reddit.com","www.reddit.com","old.reddit.com","redd.it","v.redd.it","pinterest.com","www.pinterest.com","pin.it","twitch.tv","www.twitch.tv","clips.twitch.tv","vimeo.com","www.vimeo.com","player.vimeo.com","dailymotion.com","www.dailymotion.com","dai.ly","snapchat.com","www.snapchat.com","t.snapchat.com","soundcloud.com","www.soundcloud.com","m.soundcloud.com","on.soundcloud.com"}
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

async def notify(session, chat, text, reply_id=None, thread_id=None):
    async with session.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": chat, "text": text, **({"message_thread_id":thread_id} if thread_id else {}), **({"reply_parameters":{"message_id":reply_id,"allow_sending_without_reply":True}} if reply_id else {})}) as r:
        r.raise_for_status()
        await r.read()

VIDEO_TMP=Path(__file__).resolve().parent / ".video-tmp"

def clean_video_tmp():
    VIDEO_TMP.mkdir(exist_ok=True)
    for path in VIDEO_TMP.iterdir():
        if path.name.startswith("video-") and path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
    log.info("Video temporary directory cleaned; free disk: %s MB", shutil.disk_usage(VIDEO_TMP).free//1048576)

async def process(session, url, chat, reply_id=None, thread_id=None):
    if shutil.disk_usage(VIDEO_TMP).free < 150*1048576:
        raise RuntimeError("insufficient disk space")
    with tempfile.TemporaryDirectory(prefix="video-", dir=VIDEO_TMP) as tmp:
        ffmpeg = shutil.which("ffmpeg")
        started=time.monotonic()
        audio=urlsplit(url).hostname in {"soundcloud.com","www.soundcloud.com","m.soundcloud.com","on.soundcloud.com"}
        fmt = "b[ext=mp4][height<=720]/b[ext=mp4]" if not ffmpeg else "b[ext=mp4][height<=720]/bv*[ext=mp4][height<=720]+ba[ext=m4a]/b[ext=mp4]"
        options=["-f","bestaudio","-x","--audio-format","mp3","--audio-quality","192K"] if audio else ["-f",fmt,"--merge-output-format","mp4"]
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "yt_dlp", "--concurrent-fragments", "3", "--no-playlist", "--no-cache-dir", "--no-progress", "--max-filesize", "45M", "--socket-timeout", "20", "--retries", "1", *options, "-o", str(Path(tmp)/"video.%(ext)s"), "--", url, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), 120)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        path = Path(tmp)/("video.mp3" if audio else "video.mp4")
        if proc.returncode or not path.is_file() or path.stat().st_size > 49_000_000:
            raise RuntimeError("download failed or exceeds limit")
        downloaded=time.monotonic()
        log.info("Video download+merge %.2fs; size %.2f MB",downloaded-started,path.stat().st_size/1048576)
        with path.open("rb") as f:
            data = FormData()
            data.add_field("chat_id", str(chat))
            if thread_id: data.add_field("message_thread_id",str(thread_id))
            if reply_id: data.add_field("reply_parameters",json.dumps({"message_id":reply_id,"allow_sending_without_reply":True}))
            data.add_field("audio" if audio else "video", f, filename="audio.mp3" if audio else "video.mp4", content_type="audio/mpeg" if audio else "video/mp4")
            async with session.post(f"https://api.telegram.org/bot{TOKEN}/"+("sendAudio" if audio else "sendVideo"), data=data) as r:
                result=await r.json()
                if r.status != 200 or not result.get("ok"):
                    raise RuntimeError("Telegram upload failed")
        log.info("Video upload %.2fs; processing total %.2fs",time.monotonic()-downloaded,time.monotonic()-started)

async def handle_job(session, job):
    started=time.monotonic()
    log.info("Video job claimed: %s", job.get("id"))
    if type(job.get("user_id")) is int and type(job.get("chat_id")) is int and (job["chat_id"]<0 or job["user_id"]==job["chat_id"]) and valid(job["url"]):
        chat=job["chat_id"]
        reply_id=job.get("reply_id")
        try:
            await process(session, job["url"], chat, reply_id, job.get("thread_id"))
            log.info("Video uploaded: %s", job["id"])
        except Exception as e:
            log.warning("Video processing failed: %s", type(e).__name__)
            try:
                await notify(session, chat, "⚠️ Не вдалося завантажити медіа. Можливо, публікація недоступна або файл завеликий.", reply_id, job.get("thread_id"))
            except Exception:
                pass
        if job.get("status_id"):
            try:
                async with session.post(f"https://api.telegram.org/bot{TOKEN}/deleteMessage",json={"chat_id":chat,"message_id":job["status_id"]}) as response:
                    if response.status!=200: log.warning("Video status deletion HTTP %s",response.status)
            except Exception as e: log.warning("Video status deletion failed: %s",type(e).__name__)
    for attempt in range(5):
        try:
            await request_job(session, "done", {"id":job["id"],"token":job["token"]})
            break
        except Exception:
            await asyncio.sleep(2**attempt)
    log.info('Video job total %.2fs',time.monotonic()-started)

async def dispatcher(session):
    active=set()
    idle=0
    try:
        while True:
            active={t for t in active if not t.done()}
            if len(active)>=5:
                await asyncio.wait(active,return_when=asyncio.FIRST_COMPLETED)
                continue
            try:
                job=(await request_job(session,"claim",{})).get("job")
                if job:
                    idle=0
                    active.add(asyncio.create_task(handle_job(session,job)))
                    continue
                idle+=1
                await asyncio.sleep(1 if idle<=10 else 3)
            except Exception as e:
                log.warning("Video queue polling failed: %s",type(e).__name__)
                await asyncio.sleep(10)
    finally:
        for task in active: task.cancel()
        await asyncio.gather(*active,return_exceptions=True)

async def lifecycle(app):
    clean_video_tmp()
    async with ClientSession(timeout=ClientTimeout(total=120),connector=TCPConnector(limit=20,ttl_dns_cache=300,keepalive_timeout=30)) as session:
        log.info("NebulaVideo started: 1 dispatcher, 5 processing slots, ffmpeg=%s",bool(shutil.which("ffmpeg")))
        task=asyncio.create_task(dispatcher(session))
        yield
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)

async def health(request):
    return web.json_response({"ready": True, "ffmpeg": bool(shutil.which("ffmpeg"))})

if __name__ == "__main__":
    app = web.Application(client_max_size=4096)
    app.router.add_get("/health", health)
    app.cleanup_ctx.append(lifecycle)
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("SERVER_PORT", os.getenv("PORT", "8080"))), access_log=None)
