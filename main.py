import datetime, html, wave, re, os, asyncio, time, tempfile, logging, base64, json, sqlite3
from pathlib import Path
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
load_dotenv()
logging.basicConfig(level=logging.INFO)
MODELS={'groq1':('groq',1,'whisper-large-v3'),'groq1t':('groq',1,'whisper-large-v3-turbo'),'groq2':('groq',2,'whisper-large-v3'),'groq2t':('groq',2,'whisper-large-v3-turbo'),'cf1':('cf',1,'@cf/openai/whisper-large-v3-turbo'),'cf2':('cf',2,'@cf/openai/whisper-large-v3-turbo'),'ovh':('ovh',1,'solaria-3')}
NAMES={k:f"{ {'groq':'Groq','cf':'Cloudflare','gemini':'Gemini','ovh':'OVHcloud Gladia'}[p]} #{n} • {model.split('/')[-1]}" for k,(p,n,model) in MODELS.items()}
OWNER=int(os.environ['OWNER_ID']); bot=Bot(os.environ['BOT_TOKEN']); dp=Dispatcher(); lock=asyncio.Lock()
ORDER=['ovh','groq1','groq1t','cf1','groq2','groq2t','cf2']
db=sqlite3.connect('state.db')
db.execute('CREATE TABLE IF NOT EXISTS cooldowns (provider TEXT PRIMARY KEY, until REAL)')
db.execute('CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY, enabled INTEGER)')
db.execute('INSERT OR IGNORE INTO control VALUES (1,1)')
db.execute('CREATE TABLE IF NOT EXISTS usage_daily (day TEXT, provider TEXT, attempts INTEGER, seconds INTEGER, successes INTEGER, limited INTEGER, PRIMARY KEY(day,provider))')
db.execute('CREATE TABLE IF NOT EXISTS quota_headers (provider TEXT PRIMARY KEY, info TEXT)')
db.commit()
def enabled():
    return bool(db.execute('SELECT enabled FROM control WHERE id=1').fetchone()[0])
def usage(k,seconds=0,attempt=0,success=0,limited=0):
    day=datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    db.execute('INSERT INTO usage_daily VALUES (?,?,?,?,?,?) ON CONFLICT(day,provider) DO UPDATE SET attempts=attempts+excluded.attempts, seconds=seconds+excluded.seconds, successes=successes+excluded.successes, limited=limited+excluded.limited',(day,k,attempt,seconds,success,limited))
    db.commit()
def report():
    day=datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    rows={r[0]:r[1:] for r in db.execute('SELECT provider,attempts,seconds,successes,limited FROM usage_daily WHERE day=?',(day,))}
    def duration(n):
        h,r=divmod(n,3600)
        m,sec=divmod(r,60)
        return ' '.join(([f'{h} год'] if h else [])+([f'{m} хв'] if m else [])+([f'{sec} с'] if sec or not n else []))
    total=[sum(r[i] for r in rows.values()) for i in range(4)]
    lines=['📊 <b>Статистика NebulaVoice</b>',f'📅 {day} (UTC)', '🟢 Транскрипція увімкнена' if enabled() else '🔴 Транскрипція вимкнена', '', '<b>Загальна статистика</b>',f'Успішних транскрипцій: {total[2]}',f'Спроб обробки: {total[0]}',f'Аудіо у спробах: {duration(total[1])}',f'У черзі: {waiting.qsize()} / 50',f'Активних завдань: {len(active_jobs)} / 4']
    for k in ORDER:
        a,b,c,d=rows.get(k,(0,0,0,0))
        remaining=max(0,int(blocked('quota:'+k)-time.time()))
        primary={'groq2':'groq1','groq2t':'groq1t','cf2':'cf1'}.get(k)
        state='🔴 Ліміт досягнуто' if remaining else '🟡 Обробляє аудіо' if k in active_providers else '⚪ Резерв' if primary and blocked('quota:'+primary)<=time.time() else '🟡 Тимчасова пауза' if blocked(k)>time.time() else '🟢 Готовий'
        lines.extend(['',f'<b>{html.escape(NAMES[k])}</b>',state,f'Транскрипцій: {c} | Аудіо у спробах: {duration(b)}'])
        if d: lines.append(f'Відмов через ліміт: {d}')
        if remaining: lines.append(f'Повторна перевірка через: {duration(remaining)}')
        h=db.execute('SELECT info FROM quota_headers WHERE provider=?',(k,)).fetchone()
        if h and remaining: lines.append('<blockquote expandable>'+html.escape(h[0])+'</blockquote>')
    lines.extend(['','<i>Статистика з моменту початку обліку. Тривалість включає повторні спроби. Точний залишок квот залежить від даних API.</i>'])
    return '\n'.join(lines)
def blocked(k):
    row=db.execute('SELECT until FROM cooldowns WHERE provider=?',(k,)).fetchone()
    return row[0] if row else 0
def block(k,seconds):
    db.execute('INSERT OR REPLACE INTO cooldowns VALUES (?,?)',(k,time.time()+seconds))
    db.commit()
class RateLimited(Exception):
    def __init__(self,seconds,info=''):
        self.seconds=seconds
        self.info=info
WELCOME = """🇺🇦 Привіт! Я NebulaVoice — український бот для автоматичної транскрипції!

🎙 Готовий перетворювати ваші голосові повідомлення та кружечки на текст.

<blockquote expandable><b>Мої можливості:</b>
• Автоматичне розпізнавання мовлення.
• Підтримка різних мов, суржику та сленгу.
• Транскрипція без цензури.
• Автоматичний вибір доступного ШІ.

Просто надсилайте голосові або кружечки, а решту я зроблю сам!

Для розпізнавання аудіо передається зовнішнім ШІ-сервісам.</blockquote>"""
@dp.message(Command('start'))
async def start(m:Message):
    await m.answer(WELCOME,parse_mode='HTML')
    if m.chat.type=='private' and m.from_user and m.from_user.id==OWNER: await m.answer('Панель власника',reply_markup=owner_menu())
@dp.my_chat_member()
async def joined(event:ChatMemberUpdated):
    if event.chat.type in ('group','supergroup') and event.old_chat_member.status in ('left','kicked') and event.new_chat_member.status in ('member','administrator'):
        await bot.send_message(event.chat.id,WELCOME,parse_mode='HTML')
def need(k):
    v=os.getenv(k)
    if not v: raise ValueError('Не налаштовано '+k)
    return v
async def request(s,url,headers,**kw):
    async with s.post(url,headers=headers,**kw) as r:
        if r.status==429:
            try: delay=max(60,float(r.headers.get('Retry-After','60')))
            except ValueError: delay=60
            raise RateLimited(delay, '; '.join(f'{k}: {v}' for k,v in r.headers.items() if k.lower().startswith(('x-ratelimit-','ratelimit-')) or k.lower()=='retry-after')[:300])
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
        block('ovh',31)
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
@dp.message(F.voice | F.video_note)
async def voice(m:Message):
    if m.from_user and m.from_user.is_bot: return
    if not enabled(): return await m.reply(DISABLED)
    item=m.voice or m.video_note
    if (item.file_size or 0)>10*1024*1024: return await m.reply('Файл завеликий (10 МБ).')
    if waiting.qsize()>=50: return await m.reply('Черга заповнена. Повтори пізніше.')
    status=await m.reply('⏳ У черзі...')
    if not enabled(): return await status.edit_text(DISABLED)
    try: waiting.put_nowait((m,status))
    except asyncio.QueueFull: await status.edit_text('Черга заповнена. Повтори пізніше.')

waiting=asyncio.Queue(maxsize=50)
active_providers=set()
active_jobs={}
DISABLED='⚠️ Транскрипцію тимчасово вимкнено.'
def owner_menu():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='⏸ Вимкнути' if enabled() else '▶️ Увімкнути',callback_data='owner:toggle')],[InlineKeyboardButton(text='📊 Статистика',callback_data='owner:stats')]])
async def mark_disabled(status):
    try: await status.edit_text(DISABLED)
    except Exception: pass
async def disable(reason):
    if not enabled(): return
    db.execute('UPDATE control SET enabled=0 WHERE id=1')
    db.commit()
    queued=[]
    while not waiting.empty():
        m,status=waiting.get_nowait()
        queued.append(status)
        waiting.task_done()
    current=asyncio.current_task()
    jobs=[(t,status) for t,status in active_jobs.items() if t is not current]
    for t,status in jobs: t.cancel()
    for status in queued: await mark_disabled(status)
    try: await bot.send_message(OWNER,'🔴 <b>Транскрипцію вимкнено</b>\nПричина: '+html.escape(reason)+f'\nВидалено з черги: {len(queued)}; скасовано активних: {len(jobs)}.\n\n'+report(),parse_mode='HTML',reply_markup=owner_menu())
    except Exception as e: logging.warning('Owner notification failed: %s',type(e).__name__)
@dp.callback_query(F.data.startswith('owner:'))
async def owner_action(c:CallbackQuery):
    if c.from_user.id!=OWNER or not isinstance(c.message,Message) or c.message.chat.type!='private':
        return await c.answer('Тільки власник',show_alert=True)
    await c.answer()
    if c.data=='owner:stats': return await c.message.answer(report(),parse_mode='HTML')
    if enabled(): await disable('Вимкнено власником')
    else:
        db.execute('UPDATE control SET enabled=1 WHERE id=1')
        db.commit()
    await c.message.edit_reply_markup(reply_markup=owner_menu())

async def process_voice(m,status):
    item=m.voice or m.video_note
    await status.edit_text('⏳ Транскрибую...')
    with tempfile.TemporaryDirectory() as d:
        p=Path(d)/('voice.ogg' if m.voice else 'circle.mp4')
        await bot.download(item,destination=p)
        if m.video_note:
            out=Path(d)/'audio.wav'
            proc=await asyncio.create_subprocess_exec('ffmpeg','-nostdin','-loglevel','error','-i',str(p),'-vn','-ac','1','-ar','16000',str(out),stderr=asyncio.subprocess.DEVNULL)
            if await proc.wait()!=0: raise ValueError('Не вдалося витягти аудіо')
            p=out
        order=[k for k in ORDER if k!='ovh' or item.duration<=15]
        # Secondary credentials must represent independently permitted quotas; only use after primary HTTP 429.
        deadline=time.time()+86400
        notified=False
        while time.time()<deadline and enabled():
            for k in order:
                if blocked(k)>time.time() or k in active_providers: continue
                if MODELS[k][1]==2:
                    primary={'groq2':'groq1','groq2t':'groq1t','cf2':'cf1'}[k]
                    if blocked('quota:'+primary)<=time.time(): continue
                if not enabled(): return await mark_disabled(status)
                active_providers.add(k)
                usage(k,seconds=item.duration,attempt=1)
                try:
                    result=await transcribe(k,p)
                except RateLimited as e:
                    block(k,e.seconds)
                    block('quota:'+k,e.seconds)
                    usage(k,limited=1)
                    db.execute('INSERT OR REPLACE INTO quota_headers VALUES (?,?)',(k,e.info or 'API не повідомив залишок'))
                    db.commit()
                    continue
                except Exception as e:
                    logging.warning('Provider %s failed: %s',k,type(e).__name__)
                    block(k,60)
                    continue
                finally:
                    active_providers.discard(k)
                usage(k,success=1)
                if not enabled(): return await mark_disabled(status)
                await status.edit_text('<blockquote expandable>'+html.escape(result[:3900])+'</blockquote>',parse_mode='HTML')
                for i in range(3900,len(result),3900): await m.reply('<blockquote expandable>'+html.escape(result[i:i+3900])+'</blockquote>',parse_mode='HTML')
                return
            if not active_providers and all(blocked('quota:'+k)>time.time() for k in ORDER):
                await disable('Усі сім API-профілів одночасно повернули HTTP 429; це може бути тимчасовий ліміт, не обов’язково добовий.')
                return await mark_disabled(status)
            if not notified:
                await status.edit_text('⏳ API тимчасово недоступні. Очікую поновлення лімітів.')
                notified=True
            # Avoid repeated edits of identical Telegram messages.
            delay=max(1,min(300,min((blocked(k) for k in order if blocked(k)>time.time()),default=time.time()+60)-time.time()))
            await asyncio.sleep(min(delay,1) if active_providers else delay)
        if not enabled(): return await mark_disabled(status)
        await status.edit_text('❌ Не вдалося транскрибувати протягом 24 годин.')
async def worker():
    while True:
        m,status=await waiting.get()
        try:
            if not enabled():
                await mark_disabled(status)
                continue
            job=asyncio.create_task(process_voice(m,status))
            active_jobs[job]=status
            try: await job
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling(): raise
                await mark_disabled(status)
            finally: active_jobs.pop(job,None)
        except Exception as e:
            logging.warning('Job failed: %s',type(e).__name__)
            try: await status.edit_text('❌ Не вдалося обробити аудіо.')
            except Exception: pass
        finally: waiting.task_done()
async def main():
    tasks=[asyncio.create_task(worker()) for _ in range(4)]
    try: await dp.start_polling(bot,handle_as_tasks=True)
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
if __name__=='__main__': asyncio.run(main())
