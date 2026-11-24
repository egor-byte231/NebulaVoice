import os, asyncio, time, tempfile, logging, base64, json, sqlite3
from pathlib import Path
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
load_dotenv()
logging.basicConfig(level=logging.INFO)
NAMES={'ovh':'OVHcloud AI','groq':'Groq','cf':'Cloudflare AI','gemini':'Google Gemini','hf':'Hugging Face'}
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
        if r.status>=400: raise ValueError(f'API HTTP {r.status}'+(' — ліміт або обмеження' if r.status==429 else ''))
        return await r.json(content_type=None)
async def transcribe(k,path):
    data=path.read_bytes()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
        if k=='groq':
            f=aiohttp.FormData(); f.add_field('file',data,filename=path.name,content_type='audio/ogg' if path.suffix=='.ogg' else 'audio/wav'); f.add_field('model','whisper-large-v3'); f.add_field('language','uk')
            j=await request(s,'https://api.groq.com/openai/v1/audio/transcriptions',{'Authorization':'Bearer '+need('GROQ_API_KEY')},data=f)
        elif k=='cf':
            j=await request(s,'https://api.cloudflare.com/client/v4/accounts/'+need('CF_ACCOUNT_ID')+'/ai/run/@cf/openai/whisper-large-v3-turbo',{'Authorization':'Bearer '+need('CF_API_TOKEN')},json={'audio':base64.b64encode(data).decode(),'language':'uk','task':'transcribe'}); j=j.get('result') or {}
        elif k=='gemini':
            j=await request(s,'https://generativelanguage.googleapis.com/v1beta/models/'+need('GEMINI_MODEL')+':generateContent',{'x-goog-api-key':need('GEMINI_API_KEY')},json={'contents':[{'parts':[{'text':'Transcribe this audio verbatim in its original language. Do not translate. Return only the transcript.'},{'inline_data':{'mime_type':'audio/ogg' if path.suffix=='.ogg' else 'audio/wav','data':base64.b64encode(data).decode()}}]}]}); j={'text':''.join(p.get('text','') for p in j['candidates'][0]['content']['parts'])}
        else:
            prefix='OVH' if k=='ovh' else 'HF'; url=need(prefix+'_URL'); token=os.getenv(prefix+'_TOKEN'); headers={'Authorization':'Bearer '+token} if token else {}
            f=aiohttp.FormData(); f.add_field('file',data,filename=path.name); f.add_field('language','uk'); model=os.getenv(prefix+'_MODEL')
            if model: f.add_field('model',model)
            j=await request(s,url,headers,data=f)
        text=j.get('text') if isinstance(j,dict) else None
        if not isinstance(text,str) or not text.strip(): raise ValueError('API не повернув текст')
        return text.strip()
@dp.message(F.voice | F.video_note)
async def voice(m:Message):
    if not m.from_user or m.from_user.id!=OWNER: return
    k=get(m.chat.id)
    if k not in NAMES: return
    item=m.voice or m.video_note
    if (item.file_size or 0)>10*1024*1024: return await m.reply('Файл завеликий для тестової версії (10 МБ).')
    if lock.locked(): return await m.reply('Попередній тест ще триває. Повтори пізніше.')
    async with lock:
        status=await m.reply('⏳ '+NAMES[k]); t=time.monotonic()
        try:
            with tempfile.TemporaryDirectory() as d:
                p=Path(d)/('voice.ogg' if m.voice else 'circle.mp4'); await bot.download(item,destination=p)
                if m.video_note:
                    out=Path(d)/'audio.wav'; proc=await asyncio.create_subprocess_exec('ffmpeg','-nostdin','-loglevel','error','-i',str(p),'-vn','-ac','1','-ar','16000',str(out),stderr=asyncio.subprocess.DEVNULL)
                    if await proc.wait()!=0: raise ValueError('Не вдалося витягти аудіо')
                    p=out
                text=await transcribe(k,p)
            await status.edit_text(f'✅ {NAMES[k]} | {time.monotonic()-t:.1f} с | аудіо {item.duration} с')
            for i in range(0,len(text),3900): await m.reply(text[i:i+3900])
        except ValueError as e: await status.edit_text('❌ '+NAMES[k]+': '+str(e))
        except Exception as e: await status.edit_text('❌ '+NAMES[k]+': '+type(e).__name__+' (перевір налаштування або мережу)')
async def main():
    await dp.start_polling(bot,handle_as_tasks=True)
if __name__=='__main__': asyncio.run(main())
