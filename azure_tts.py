"""Local Piper TTS with four bounded concurrent jobs."""
import asyncio
import os
import tempfile
import re
import json
from pathlib import Path
from aiogram.types import FSInputFile, ReplyParameters

slots = asyncio.Semaphore(4)
pending = 0
download_lock = asyncio.Lock()

# Experimental presets; only applied to the Ukrainian Mykyta model.
PRESETS = {
    'natural': (['--length-scale','1.04','--sentence-silence','0.22'], 'highpass=f=65,equalizer=f=3200:t=q:w=1:g=-1.5,acompressor=threshold=0.15:ratio=1.5'),
    'deep': (['--length-scale','1.07','--sentence-silence','0.25'], 'asetrate=20948,aresample=22050,atempo=1.05263,highpass=f=60,equalizer=f=180:t=q:w=1:g=2'),
    'soft': (['--length-scale','1.10','--sentence-silence','0.30'], 'highpass=f=65,equalizer=f=3500:t=q:w=1:g=-3,acompressor=threshold=0.12:ratio=1.7'),
}

async def select_model(text, preference='mykyta'):
    if preference in PRESETS:
        preference = 'mykyta'
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
    # Third-party Serhiy is installed separately, not listed in Piper's catalog.
    if preference == "serhiy":
        model = directory / "uk_UA-serhiy-high.onnx"
        return str(model) if model.is_file() and Path(str(model) + ".json").is_file() else None
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

def normalize_ukrainian_tts(text):
    """Prepare Ukrainian text for speech without altering the visible AI answer."""
    import html
    text = html.unescape(text)
    # Remove markup delimiters while keeping their spoken content.
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[([^\]]+)\]\(https?://[^)\s]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "", text)
    text = re.sub(r"(?m)^\s*>\s*", "", text)
    text = re.sub(r"[*_~\`]+", "", text)
    # Quotes and decorative symbols should not be read aloud.
    text = re.sub(r"[«»„“”‟\"'‘’‚‹›]", "", text)
    text = re.sub(r"[#@©®™•▪●★☆✓✔✅❌🔊🎙️]", " ", text)
    # Preserve ordinary punctuation for natural pauses.
    text = re.sub(
        r"(?<!\w)20[\s\-–‑]*словн(ого|ому|ими|их|им|ім|ий|ої|ій|ою|а|у|е|і)(?!\w)",
        lambda match: "двадцятисловн" + match.group(1),
        text, flags=re.IGNORECASE,
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

async def speak(message, text, preference='mykyta', reply_to_message_id=None):
    global pending
    if pending >= 32:
        return await message.reply("Черга озвучування заповнена. Спробуй пізніше.")
    pending += 1
    try:
        async with slots:
            model = await select_model(text, preference)
            if not model:
                if preference == 'serhiy':
                    return await message.reply('Голос Сергій ще не встановлений на VPS. Завантаж модель командою встановлення.')
                return await message.reply('Для цієї мови немає доступної голосової моделі Piper.')
            with tempfile.TemporaryDirectory(prefix="nebula-tts-") as directory:
                wav = str(Path(directory) / "speech.wav")
                ogg = str(Path(directory) / "speech.ogg")
                options, audio_filter = PRESETS.get(preference, ([], None)) if Path(model).name.startswith("uk_UA-mykyta-") else ([], None)
                await run_process(os.getenv("PIPER_BIN", "piper"), "--model", model, "--output_file", wav, *options, data=normalize_ukrainian_tts(text[:3700]).encode("utf-8"))
                if preference == "deep" and audio_filter:
                    import wave
                    with wave.open(wav, "rb") as stream:
                        rate = stream.getframerate()
                    audio_filter = audio_filter.replace("20948", str(round(rate*0.95))).replace("22050", str(rate))
                # Accelerate every Piper voice by 10% while preserving pitch.
                # Apply after voice-specific EQ so this also covers original voices.
                speed_filter = "atempo=1.10"
                audio_filter = (audio_filter + "," + speed_filter) if audio_filter else speed_filter
                filters = ["-af", audio_filter]
                await run_process("ffmpeg", "-nostdin", "-loglevel", "error", "-i", wav, *filters, "-c:a", "libopus", "-threads", "1", ogg)
                await message.bot.send_voice(
                    chat_id=message.chat.id,
                    voice=FSInputFile(ogg),
                    reply_parameters=ReplyParameters(message_id=reply_to_message_id or message.message_id),
                )
    finally:
        pending -= 1
