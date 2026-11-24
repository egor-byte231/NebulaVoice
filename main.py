import wave, re, os, asyncio, time, tempfile, logging, base64, json, sqlite3
from pathlib import Path
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
load_dotenv()
logging.basicConfig(level=logging.INFO)
MODELS={'groq1':('groq',1,'whisper-large-v3'),'groq1t':('groq',1,'whisper-large-v3-turbo'),'groq2':('groq',2,'whisper-large-v3'),'groq2t':('groq',2,'whisper-large-v3-turbo'),'cf1':('cf',1,'@cf/openai/whisper-large-v3-turbo'),'cf2':('cf',2,'@cf/openai/whisper-large-v3-turbo'),'ovh':('ovh',1,'solaria-3')}
NAMES={k:f"{ {'groq':'Groq','cf':'Cloudflare','gemini':'Gemini','ovh':'OVHcloud Gladia'}[p]} #{n} • {model.split('/')[-1]}" for k,(p,n,model) in MODELS.items()}
OWNER=int(os.environ['OWNER_ID']); bot=Bot(os.environ['BOT_TOKEN']); dp=Dispatcher(); lock=asyncio.Lock()
db=sqlite3.connect('state.db'); db.execute('CREATE TABLE IF NOT EXISTS settings (chat INTEGER PRIMARY KEY, provider TEXT)'); db.commit()
def get(chat):
    row=db.execute('SELECT provider FROM settings WHERE chat=?',(chat,)).fetchone(); return row[0] if row else None
def menu(chat):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=('✓ ' if get(chat)==k else '')+v,callback_data='pick:'+k)] for k,v in NAMES.items()]+[[InlineKeyboardButton(text='⏸ Вимкнути',callback_data='pick:off')]])
@dp.message(Command('start','test'))
async def start(m:Message):
    if m.from_user and m.from_user.id==OWNER: await m.answer('NebulaVoice • Тестування\nВибери один API. Автоматичного перемикання немає.',reply_markup=menu(m.chat.id))
@dp.callback_query(F.data.startswith('pick:'))
async def pick(c:CallbackQuery):
    if c.from_user.id!=OWNER: return await c.answer('Тільки власник',show_alert=True)
    k=c.data.split(':')[1]
    if k not in NAMES and k!='off': return await c.answer('Невідомий API')
    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(c.message.chat.id,k)); db.commit()
    await c.message.edit_reply_markup(reply_markup=menu(c.message.chat.id)); await c.answer(NAMES.get(k,'Вимкнено'))
def need(k):
    v=os.getenv(k)
    if not v: raise ValueError('Не налаштовано '+k)
    return v
async def request(s,url,headers,**kw):
    async with s.post(url,headers=headers,**kw) as r:
        if r.status>=400:
            message=f'API HTTP {r.status}'+(' — ліміт або обмеження' if r.status==429 else '')
            if url.startswith('https://generativelanguage.googleapis.com/'):
                try:
                    payload=await r.json(content_type=None)
                    err=payload.get('error',{})
                    parts=[str(err.get('status','')),str(err.get('message',''))]
                    for detail in err.get('details',[]):
                        if isinstance(detail,dict):
                            parts.extend(str(detail.get(key,'')) for key in ('reason','domain'))
                    diagnostic=' | '.join(p for p in parts if p)
                    for key,value in os.environ.items():
                        if value and ('TOKEN' in key or 'KEY' in key or 'SECRET' in key):
                            diagnostic=diagnostic.replace(value,'[REDACTED]')
                    diagnostic=re.sub(r'(?i)(?:AIza|gsk_|cfut_|AQ\\.)[A-Za-z0-9_.-]+','[REDACTED]',diagnostic)
                    message+='\n'+(diagnostic[:1800] or 'Google не повернув опис помилки')
                except Exception:
                    message+='\nGoogle не повернув доступний JSON-опис помилки'
            raise ValueError(message)
        return await r.json(content_type=None)
ASR_PROMPT = "Дослівна розмовна транскрипція: українська, русский язык, суржик, сленг, матюки. Без перекладу, цензури, виправлення граматики чи вигаданих слів. Зберігати перемикання мов."
GEMINI_PROMPT = "Transcribe verbatim in the original spoken languages. Preserve Ukrainian, Russian, Surzhyk, code-switching, slang, profanity and grammatical mistakes. Do not translate, censor, normalize or invent words. Return only the transcript."
async def ovh_transcribe(s,path):
    pcm=path.with_name('ovh.wav')
    proc=await asyncio.create_subprocess_exec('ffmpeg','-nostdin','-loglevel','error','-i',str(path),'-ar','16000','-ac','1','-c:a','pcm_s16le',str(pcm),stderr=asyncio.subprocess.DEVNULL)
    if await proc.wait()!=0: raise ValueError('Не вдалося конвертувати аудіо для OVH')
    with wave.open(str(pcm),'rb') as audio:
        if audio.getnframes()>15*16000: raise ValueError('OVH: максимум 15 секунд')
        session=await request(s,'https://oai.endpoints.kepler.ai.cloud.ovh.net/gladia/v2/live?region=eu-west',{},json={'model':'solaria-3','encoding':'wav/pcm','sample_rate':16000,'bit_depth':16,'channels':1,'messages_config':{'receive_final_transcripts':True}})
        url=session['url']
        if not url.startswith('wss://'): raise ValueError('OVH: некоректна адреса сесії')
        finals=[]
        async with s.ws_connect(url) as ws:
            async def send():
                while chunk:=audio.readframes(1600):
                    await ws.send_bytes(chunk)
                    await asyncio.sleep(len(chunk)/32000)
                await ws.send_json({'type':'stop_recording'})
            async def receive():
                async for msg in ws:
                    if msg.type==aiohttp.WSMsgType.TEXT:
                        event=json.loads(msg.data)
                        data=event.get('data') or {}
                        if event.get('type')=='transcript' and data.get('is_final'):
                            finals.append(data.get('utterance',{}).get('text',''))
                        if event.get('type')=='error': raise ValueError('OVH: помилка live-сесії')
                    elif msg.type==aiohttp.WSMsgType.ERROR: raise ValueError('OVH: помилка WebSocket')
            tasks=[asyncio.create_task(send()),asyncio.create_task(receive())]
            try:
                await asyncio.wait_for(asyncio.gather(*tasks),timeout=100)
            finally:
                for task in tasks: task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
        return {'text':' '.join(finals)}
async def transcribe(k,path):
    provider,n,model=MODELS[k]
    data=path.read_bytes()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
        if provider=='ovh':
            j=await ovh_transcribe(s,path)
        elif provider=='groq':
            f=aiohttp.FormData(); f.add_field('file',data,filename=path.name,content_type='audio/ogg' if path.suffix=='.ogg' else 'audio/wav'); f.add_field('model',model); f.add_field('temperature','0'); f.add_field('prompt',ASR_PROMPT)
            j=await request(s,'https://api.groq.com/openai/v1/audio/transcriptions',{'Authorization':'Bearer '+need(f'GROQ_API_KEY_{n}')},data=f)
        elif provider=='cf':
            j=await request(s,'https://api.cloudflare.com/client/v4/accounts/'+need(f'CF_ACCOUNT_ID_{n}')+'/ai/run/'+model,{'Authorization':'Bearer '+need(f'CF_API_TOKEN_{n}')},json={'audio':base64.b64encode(data).decode(),'task':'transcribe','initial_prompt':ASR_PROMPT,'condition_on_previous_text':False,'vad_filter':True}); j=j.get('result') or {}
        elif provider=='gemini':
            j=await request(s,'https://generativelanguage.googleapis.com/v1beta/models/'+model+':generateContent',{'x-goog-api-key':need('GEMINI_API_KEY')},json={'generationConfig':{'temperature':0},'contents':[{'parts':[{'text':GEMINI_PROMPT},{'inline_data':{'mime_type':'audio/ogg' if path.suffix=='.ogg' else 'audio/wav','data':base64.b64encode(data).decode()}}]}]}); j={'text':''.join(p.get('text','') for p in j['candidates'][0]['content']['parts'])}
        text=j.get('text') if isinstance(j,dict) else None
        if not isinstance(text,str) or not text.strip(): raise ValueError('API не повернув текст')
        return text.strip()
@dp.message(Command('transcribe'))
async def manual(m:Message):
    if not m.from_user or m.from_user.id!=OWNER: return
    target=m.reply_to_message
    if not target or not (target.voice or target.video_note):
        return await m.reply('Відповідай командою /transcribe на голосове або кружечок.')
    if get(m.chat.id) not in NAMES:
        return await m.reply('Спочатку вибери модель через /test.')
    await target.reply('Готовий до транскрипції.',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🎙 Транскрибувати',callback_data='transcribe')]]))

@dp.callback_query(F.data=='transcribe')
async def manual_click(c:CallbackQuery):
    if c.from_user.id!=OWNER: return await c.answer('Тільки власник',show_alert=True)
    if not isinstance(c.message,Message): return await c.answer('Повідомлення недоступне',show_alert=True)
    target=c.message.reply_to_message
    if not target or not (target.voice or target.video_note): return await c.answer('Оригінал недоступний',show_alert=True)
    if lock.locked(): return await c.answer('Попередній тест ще триває',show_alert=True)
    if get(c.message.chat.id) not in NAMES: return await c.answer('Вибери модель через /test',show_alert=True)
    await c.answer()
    await process_voice(target,c.message)

@dp.message(F.voice | F.video_note)
async def voice(m:Message):
    if not m.from_user or m.from_user.id!=OWNER: return
    if get(m.chat.id) not in NAMES: return
    await m.reply('Готовий до транскрипції.',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🎙 Транскрибувати',callback_data='transcribe')]]))

async def process_voice(m:Message,status:Message):
    k=get(m.chat.id)
    if k not in NAMES: return
    item=m.voice or m.video_note
    if k=='ovh' and item.duration>15: return await status.edit_text('OVHcloud: максимум 15 секунд. Вибери іншу модель.',reply_markup=None)
    if (item.file_size or 0)>10*1024*1024: return await m.reply('Файл завеликий для тестової версії (10 МБ).')
    if lock.locked(): return await m.reply('Попередній тест ще триває. Повтори пізніше.')
    async with lock:
        await status.edit_text('⏳ Транскрибую...',reply_markup=None); t=time.monotonic()
        try:
            with tempfile.TemporaryDirectory() as d:
                p=Path(d)/('voice.ogg' if m.voice else 'circle.mp4'); await bot.download(item,destination=p)
                if m.video_note:
                    out=Path(d)/'audio.wav'; proc=await asyncio.create_subprocess_exec('ffmpeg','-nostdin','-loglevel','error','-i',str(p),'-vn','-ac','1','-ar','16000',str(out),stderr=asyncio.subprocess.DEVNULL)
                    if await proc.wait()!=0: raise ValueError('Не вдалося витягти аудіо')
                    p=out
                text=await transcribe(k,p)
            await status.edit_text('📝 '+text[:3900],reply_markup=None)
            for i in range(3900,len(text),3900): await m.reply(text[i:i+3900])
        except ValueError as e: await status.edit_text('❌ '+NAMES[k]+': '+str(e))
        except Exception as e: await status.edit_text('❌ '+NAMES[k]+': '+type(e).__name__+' (перевір налаштування або мережу)')
async def main():
    await dp.start_polling(bot,handle_as_tasks=True)
if __name__=='__main__': asyncio.run(main())
