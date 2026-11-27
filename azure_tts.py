"""Local Piper TTS with four bounded concurrent jobs."""
import asyncio
import os
import tempfile
import re
import json
from pathlib import Path
from aiogram.types import FSInputFile

slots = asyncio.Semaphore(4)
pending = 0
download_lock = asyncio.Lock()

async def select_model(text, preference='mykyta'):
    # Language detection runs in the isolated Piper environment.
    python = str(Path(os.getenv("PIPER_BIN", "piper")).with_name("python"))
    proc = await asyncio.create_subprocess_exec(python, "-c",
        "import sys; from langdetect import detect, DetectorFactory; DetectorFactory.seed=0; print(detect(sys.stdin.read()))",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(text.encode("utf-8")), 15)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    lang = out.decode().strip().lower() if proc.returncode == 0 else "uk"
    if re.search(r"[іїєґ]", text, re.I) and lang in ("uk", "ru", "bg", "mk", "be"):
        lang = "uk"
    lang = lang.split("-")[0]
    directory = Path(os.environ["PIPER_MODELS_DIR"]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    async with download_lock:
        preferred = {"uk":"uk_UA-"+preference+"-high", "ru":"ru_RU-dmitri-medium", "en":"en_US-ryan-high"}
        voice = preferred.get(lang)
        if voice:
            model = directory / (voice + ".onnx")
            if model.is_file() and Path(str(model)+".json").is_file():
                return str(model)
        else:
            for model in sorted(directory.glob("*.onnx")):
                if model.name.split("_")[0] == lang and Path(str(model)+".json").is_file():
                    return str(model)
        # Discover official voice identifiers from Piper's catalog.
        import urllib.request
        def catalog():
            with urllib.request.urlopen("https://huggingface.co/rhasspy/piper-voices/raw/main/voices.json", timeout=20) as response:
                return json.load(response)
        voices = await asyncio.to_thread(catalog)
        candidates = [key for key in voices if key.split("_")[0] == lang]
        if not candidates:
            return None
        if lang == "uk" and voice not in candidates:
            matching = [key for key in candidates if key.startswith("uk_UA-"+preference+"-")]
            if not matching:
                return None
            voice = sorted(matching, key=lambda key: (0 if key.endswith("-high") else 1 if key.endswith("-medium") else 2 if key.endswith("-low") else 3, key))[0]
        if voice not in candidates:
            voice = sorted(candidates, key=lambda key: (0 if key.endswith("-low") else 1 if key.endswith("-x_low") else 2 if key.endswith("-medium") else 3, key))[0]
        model = directory / (voice + ".onnx")
        if model.is_file() and Path(str(model)+".json").is_file():
            return str(model)
        await run_process(python, "-m", "piper.download_voices", "--download-dir", str(directory), voice)
        model = directory / (voice + ".onnx")
        return str(model) if model.is_file() and Path(str(model)+".json").is_file() else None


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

async def speak(message, text, preference='mykyta'):
    global pending
    if pending >= 32:
        return await message.reply("Черга озвучування заповнена. Спробуй пізніше.")
    pending += 1
    try:
        async with slots:
            model = await select_model(text, preference)
            if not model:
                return await message.reply('Для цієї мови немає доступної голосової моделі Piper.')
            with tempfile.TemporaryDirectory(prefix="nebula-tts-") as directory:
                wav = str(Path(directory) / "speech.wav")
                ogg = str(Path(directory) / "speech.ogg")
                await run_process(os.getenv("PIPER_BIN", "piper"), "--model", model, "--output_file", wav, data=text[:3700].encode("utf-8"))
                await run_process("ffmpeg", "-nostdin", "-loglevel", "error", "-i", wav, "-c:a", "libopus", "-threads", "1", ogg)
                await message.reply_voice(FSInputFile(ogg))
    finally:
        pending -= 1
