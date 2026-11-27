"""Local Piper TTS with four bounded concurrent jobs."""
import asyncio
import os
import tempfile
from pathlib import Path
from aiogram.types import FSInputFile

slots = asyncio.Semaphore(4)
pending = 0

async def run_process(*args, data=None):
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.communicate(data), 90)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode:
        raise RuntimeError("TTS subprocess failed")

async def speak(message, text):
    global pending
    model = os.getenv("PIPER_MODEL", "")
    if not model or not Path(model).is_file():
        return await message.reply("Голосова модель ще не встановлена.")
    if pending >= 32:
        return await message.reply("Черга озвучування заповнена. Спробуй пізніше.")
    pending += 1
    try:
        async with slots:
            with tempfile.TemporaryDirectory(prefix="nebula-tts-") as directory:
                wav = str(Path(directory) / "speech.wav")
                ogg = str(Path(directory) / "speech.ogg")
                await run_process(os.getenv("PIPER_BIN", "piper"), "--model", model, "--output_file", wav, data=text[:2000].encode("utf-8"))
                await run_process("ffmpeg", "-nostdin", "-loglevel", "error", "-i", wav, "-c:a", "libopus", "-threads", "1", ogg)
                await message.reply_voice(FSInputFile(ogg))
    finally:
        pending -= 1
