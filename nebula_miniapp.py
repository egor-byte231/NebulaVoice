"""Telegram entry point and persistent storage for the NebulaVoice Mini App."""
import hashlib
import hmac
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


PERSONAL_BONUS = 4000
GROUP_BONUS = 10000


class MiniApp:
    def __init__(self, bot, query, daily_usage, daily_limit):
        self.bot = bot
        self.query = query
        self.daily_usage = daily_usage
        self.daily_limit = daily_limit
        self.url = os.getenv('NEBULA_MINIAPP_URL', '').rstrip('/')
        self.name = os.getenv('NEBULA_MINIAPP_SHORT_NAME', 'nebula')
        self.secret = hmac.digest(bot.token.encode(), b'nebula-miniapp-context-v1', 'sha256')
        self.username = None
        self.runner = None

    async def start(self):
        if not self.url:
            return
        if not self.url.startswith('https://') or not re.fullmatch(r'[A-Za-z0-9_]{3,30}', self.name):
            raise ValueError('Configure a public HTTPS Mini App URL and a valid BotFather short name')
        # Keep the old bonus table for rollback. The current campaign starts with an empty ledger.
        for statement in (
            'CREATE TABLE IF NOT EXISTS transcript_numbers (id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, message_id INTEGER NOT NULL, UNIQUE(chat,message_id))',
            'CREATE TABLE IF NOT EXISTS transcript_diagnostics (number INTEGER PRIMARY KEY, data TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS runtime_results (chat INTEGER, message_id INTEGER, kind TEXT, role TEXT, completed INTEGER, PRIMARY KEY(chat,message_id,kind))',
            'CREATE TABLE IF NOT EXISTS text_diagnostics (number INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER, message_id INTEGER, data TEXT NOT NULL, UNIQUE(chat,message_id))',
            'CREATE TABLE IF NOT EXISTS mini_write_prompt_results (user_id INTEGER PRIMARY KEY)',
            'CREATE TABLE IF NOT EXISTS mini_bonus_v2 (chat INTEGER PRIMARY KEY, balance INTEGER NOT NULL CHECK(balance>=0))',
            """CREATE TABLE IF NOT EXISTS mini_users (
                id INTEGER PRIMARY KEY, language TEXT NOT NULL DEFAULT 'auto',
                style TEXT NOT NULL DEFAULT 'default', notifications INTEGER NOT NULL DEFAULT 0,
                referred_by INTEGER, write_allowed INTEGER NOT NULL DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS mini_events (
                chat INTEGER NOT NULL, message INTEGER NOT NULL, kind TEXT NOT NULL,
                author INTEGER NOT NULL, at INTEGER NOT NULL, body TEXT NOT NULL,
                tokens INTEGER NOT NULL, PRIMARY KEY(chat,message,kind))""",
            'CREATE INDEX IF NOT EXISTS mini_events_author ON mini_events(author,chat,at DESC)',
            'CREATE INDEX IF NOT EXISTS mini_users_referrer ON mini_users(referred_by)',
        ):
            await self.query(statement)
        username = (await self.bot.get_me()).username
        app = web.Application(client_max_size=32 * 1024)
        app.router.add_get('/', self.page)
        app.router.add_post('/api', self.api)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        try:
            await web.TCPSite(self.runner, '127.0.0.1', int(os.getenv('NEBULA_MINIAPP_PORT', '8780'))).start()
        except BaseException:
            await self.close()
            raise
        self.username = username

    async def close(self):
        self.username = None
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    def sign(self, context):
        return hmac.new(self.secret, context.encode(), hashlib.sha256).hexdigest()[:24]

    def link(self, context):
        return f'https://t.me/{self.username}/{self.name}?startapp={context}_{self.sign(context)}'

    def keyboard(self, message):
        if not self.username:
            return None
        # A signed group ID plus Telegram's signed launch context is required on the server.
        chat = message.chat.id
        context = f'g{abs(chat)}' if chat < 0 else 'private'
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='🚀 NebulaVoice Mini App', url=self.link(context))
        ]])

    def authenticate(self, data):
        if not isinstance(data, str):
            raise web.HTTPUnauthorized()
        pairs = parse_qsl(data, keep_blank_values=True, max_num_fields=40)
        fields = dict(pairs)
        supplied = fields.pop('hash', '')
        if len(fields) != len(pairs) - 1 or not re.fullmatch('[0-9a-f]{64}', supplied):
            raise web.HTTPUnauthorized()
        check = '\n'.join(f'{key}={value}' for key, value in sorted(fields.items()))
        secret = hmac.digest(b'WebAppData', self.bot.token.encode(), 'sha256')
        if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), supplied):
            raise web.HTTPUnauthorized()
        user = json.loads(fields['user'])
        age = time.time() - int(fields['auth_date'])
        if not -30 <= age <= 3600 or type(user.get('id')) is not int or user['id'] <= 0:
            raise web.HTTPUnauthorized()
        return user, fields

    async def scope(self, user_id, fields):
        context, _, signature = fields.get('start_param', '').rpartition('_')
        if not hmac.compare_digest(self.sign(context), signature):
            return user_id, False, None
        chat_type = fields.get('chat_type')
        if context == 'private':
            return user_id, chat_type in ('private', 'sender', 'bot'), None
        group = re.fullmatch(r'g([1-9][0-9]{0,15})', context)
        if group and chat_type in ('group', 'supergroup') and fields.get('chat_instance'):
            chat = -int(group[1])
            if await self.is_group_member(chat, user_id):
                return chat, True, None
        referral = re.fullmatch(r'r([1-9][0-9]{0,15})', context)
        if referral and int(referral[1]) != user_id:
            return user_id, False, int(referral[1])
        return user_id, False, None

    async def group_member(self, chat, user_id):
        try:
            return await self.bot.get_chat_member(chat, user_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            return None

    async def is_group_member(self, chat, user_id):
        member = await self.group_member(chat, user_id)
        return member is not None and (
            member.status in ('member', 'administrator', 'creator') or (
                member.status == 'restricted' and member.is_member
            )
        )

    async def is_group_owner(self, chat, user_id):
        member = await self.group_member(chat, user_id)
        return member is not None and member.status == 'creator'

    def bonus_link(self, chat, user_id):
        context = f'b{user_id}_g{abs(chat) if chat < 0 else 0}'
        return f'https://t.me/{self.username}?start={context}_{self.sign(context)}'

    async def redeem_start(self, message, parameter):
        context, _, signature = parameter.rpartition('_')
        grant = re.fullmatch(r'b([1-9][0-9]{0,15})_g(0|[1-9][0-9]{0,15})', context)
        if not self.username or not grant or not message.from_user or message.chat.type != 'private' or (
            not hmac.compare_digest(self.sign(context), signature) or int(grant[1]) != message.from_user.id
        ):
            return 'Це посилання на бонус недійсне. Відкрий Mini App і натисни «Написати боту».'
        user_id = message.from_user.id
        chat = -int(grant[2]) if grant[2] != '0' else user_id
        grants = [(user_id, PERSONAL_BONUS)]
        if chat < 0 and await self.is_group_owner(chat, user_id):
            grants.append((chat, GROUP_BONUS))
        values = ','.join('(?,?)' for _ in grants)
        rows = await self.query(
            f'INSERT OR IGNORE INTO mini_bonus_v2(chat,balance) VALUES {values} RETURNING chat,balance',
            tuple(value for grant in grants for value in grant),
        )
        credited = {row['chat']: row['balance'] for row in rows}
        lines = []
        for target, label in ((user_id, 'особистий баланс'), (chat, 'баланс групи') if chat < 0 else (None, '')):
            if target in credited:
                amount = f"{credited[target]:,}".replace(',', ' ')
                lines.append(f'{amount} токенів на {label}.')
        if not lines:
            return 'Цей одноразовий бонус уже нараховано.'
        return '🎁 Ви отримали бонус!\n' + '\n'.join(lines)

    async def balance(self, chat):
        rows = await self.query('SELECT balance FROM mini_bonus_v2 WHERE chat=?', (chat,))
        return rows[0]['balance'] if rows else 0

    async def spend(self, chat, tokens):
        if tokens <= 0 or not self.username:
            return 0
        # The assignment and predicate are evaluated in one database write.
        rows = await self.query(
            'UPDATE mini_bonus_v2 SET balance=balance-? WHERE chat=? AND balance>=? RETURNING balance',
            (tokens, chat, tokens),
        )
        if rows:
            return tokens
        remaining = min(tokens, await self.balance(chat))
        if remaining and remaining < tokens:
            rows = await self.query(
                'UPDATE mini_bonus_v2 SET balance=0 WHERE chat=? AND balance=? RETURNING balance',
                (chat, remaining),
            )
            if rows:
                return remaining
        return 0

    async def record(self, message, kind, body='', tokens=0):
        if not self.username or not message.from_user or message.sender_chat:
            return
        try:
            await self.query(
                'INSERT OR IGNORE INTO mini_events VALUES (?,?,?,?,?,?,?)',
                (message.chat.id, message.message_id, kind, message.from_user.id,
                 int(message.date.timestamp()), body[:10000], tokens),
            )
        except Exception:
            logging.exception('Mini App event storage failed')

    async def preference(self, user_id):
        if not self.username:
            return ''
        try:
            rows = await self.query('SELECT language,style FROM mini_users WHERE id=?', (user_id,))
        except Exception:
            logging.exception('Mini App preference lookup failed')
            return ''
        if not rows:
            return ''
        row = rows[0]
        language = {'uk': 'Відповідай українською.', 'ru': 'Відповідай російською.'}.get(row['language'], '')
        style = 'Відповідай максимально стисло.' if row['style'] == 'short' else ''
        return ' '.join(filter(None, (language, style)))

    async def write_access(self, message):
        if self.username and message.from_user and message.chat.type == 'private':
            await self.query(
                'INSERT INTO mini_users(id,write_allowed) VALUES (?,1) ON CONFLICT(id) DO UPDATE SET write_allowed=1',
                (message.from_user.id,),
            )

    async def notify_limit(self, message, chat):
        if not self.username or chat < 0:
            return
        try:
            rows = await self.query(
                'SELECT 1 FROM mini_users WHERE id=? AND write_allowed=1 AND notifications=1', (chat,),
            )
            if rows:
                await self.bot.send_message(chat, '🔋 Добовий ліміт ШІ та бонусний баланс вичерпано.',
                                            reply_markup=self.keyboard(message))
        except (TelegramBadRequest, TelegramForbiddenError):
            await self.query('UPDATE mini_users SET write_allowed=0 WHERE id=?', (chat,))
        except Exception:
            logging.exception('Mini App notification failed')

    async def page(self, _request):
        return web.FileResponse(Path(__file__).with_name('nebula_miniapp.html'),
                                headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

    async def api(self, request):
        try:
            payload = await request.json()
            user, fields = self.authenticate(payload['initData'])
        except (AttributeError, KeyError, ValueError, TypeError, OverflowError):
            raise web.HTTPUnauthorized() from None
        uid = user['id']
        chat, claim, referral = await self.scope(uid, fields)
        await self.query(
            'INSERT OR IGNORE INTO mini_users(id,referred_by,write_allowed) VALUES (?,(SELECT id FROM mini_users WHERE id=?),?)',
            (uid, referral, int(user.get('allows_write_to_pm') is True)),
        )
        if user.get('allows_write_to_pm') is True:
            await self.query('UPDATE mini_users SET write_allowed=1 WHERE id=?', (uid,))
        settings = payload.get('settings')
        if settings is not None:
            if not isinstance(settings, dict):
                raise web.HTTPBadRequest()
            language, style, notifications = (settings.get(key) for key in ('language', 'style', 'notifications'))
            if language not in ('auto', 'uk', 'ru') or style not in ('default', 'short') or type(notifications) is not bool:
                raise web.HTTPBadRequest()
            await self.query('UPDATE mini_users SET language=?,style=?,notifications=? WHERE id=?',
                             (language, style, int(notifications), uid))
        profile = (await self.query('SELECT language,style,notifications,write_allowed FROM mini_users WHERE id=?', (uid,)))[0]
        bonus = await self.query('SELECT chat,balance FROM mini_bonus_v2 WHERE chat IN (?,?)', (uid, chat))
        balances = {str(row['chat']): row['balance'] for row in bonus}
        group_bonus_available = (
            claim and chat < 0 and str(chat) not in balances and await self.is_group_owner(chat, uid)
        )
        personal_bonus_available = claim and str(uid) not in balances
        bonus_url = self.bonus_link(chat if group_bonus_available else uid, uid) if (
            group_bonus_available or personal_bonus_available
        ) else None
        if payload.get('writePromptFinished') is True:
            await self.query('INSERT OR IGNORE INTO mini_write_prompt_results VALUES (?)', (uid,))
        prompt_write_access = False
        if payload.get('checkWritePrompt') is True and user.get('allows_write_to_pm') is not True:
            prompt_write_access = not await self.query(
                'SELECT 1 FROM mini_write_prompt_results WHERE user_id=?', (uid,)
            )
        quotas = {}
        for scope in (uid, chat):
            limit = self.daily_limit(scope)
            used = self.daily_usage(scope)
            free_limit = 10000 if scope > 0 else 40000
            quotas[str(scope)] = {
                'limit': limit, 'remaining': max(0, limit - used),
                'plan': 'Premium' if limit > free_limit else 'Free',
            }

        history = await self.query(
            """WITH entries AS (
                SELECT message,at,body FROM mini_events
                WHERE author=? AND chat=? AND kind='audio'
                UNION ALL
                SELECT n.message_id,COALESCE(r.completed,
                    CAST(CASE WHEN json_valid(d.data) THEN json_extract(d.data,'$.queued') END AS INTEGER)),NULL
                FROM transcript_numbers n
                LEFT JOIN transcript_diagnostics d ON d.number=n.id
                LEFT JOIN (SELECT message_id,MAX(completed) AS completed FROM runtime_results
                           WHERE chat=? AND kind IN ('audio','video') GROUP BY message_id) r
                    ON r.message_id=n.message_id
                WHERE n.chat=? AND n.chat>0 AND NOT EXISTS (
                    SELECT 1 FROM mini_events e WHERE e.chat=n.chat AND e.message=n.message_id AND e.kind='audio')
            )
            SELECT entries.*,n.id AS number FROM entries
            LEFT JOIN transcript_numbers n ON n.chat=? AND n.message_id=entries.message
            ORDER BY entries.message DESC LIMIT 50""",
            (uid, chat, chat, chat, chat),
        )
        stats = await self.query(
            """WITH audio AS (
                SELECT message_id FROM transcript_numbers WHERE chat=?
                UNION SELECT message_id FROM runtime_results WHERE chat=? AND kind IN ('audio','video')
                UNION SELECT message FROM mini_events WHERE chat=? AND kind='audio'
            ), answers AS (
                SELECT message_id FROM runtime_results WHERE chat=? AND kind='text'
                UNION SELECT message_id FROM text_diagnostics WHERE chat=? AND
                    CASE WHEN json_valid(data) THEN json_extract(data,'$.selected') END IS NOT NULL
                UNION SELECT message FROM mini_events WHERE chat=? AND kind='text'
            ), token_records AS (
                SELECT message_id, CAST(CASE WHEN json_valid(data) THEN json_extract(data,'$.user_tokens') END AS INTEGER) AS tokens
                FROM text_diagnostics WHERE chat=?
                UNION ALL SELECT message,tokens FROM mini_events WHERE chat=? AND kind='text'
            )
            SELECT 'audio' AS kind,COUNT(*) AS count,0 AS tokens FROM audio
            UNION ALL SELECT 'text',COUNT(*),COALESCE((
                SELECT SUM(tokens) FROM (SELECT MAX(tokens) AS tokens FROM token_records GROUP BY message_id)
            ),0) FROM answers""",
            (chat,) * 8,
        )
        referrals = await self.query('SELECT COUNT(*) AS count FROM mini_users WHERE referred_by=?', (uid,))
        return web.json_response({
            'name': user.get('first_name') or 'друже',
            'settings': profile, 'bonus': balances,
            'uid': str(uid), 'chat': str(chat), 'history': history, 'stats': stats,
            'quotas': quotas, 'bonusStartUrl': bonus_url,
            'bonusOffer': {'personal': PERSONAL_BONUS if personal_bonus_available else 0,
                           'group': GROUP_BONUS if group_bonus_available else 0}, 'promptWriteAccess': prompt_write_access,
            'dailyUsed': self.daily_usage(chat), 'dailyLimit': self.daily_limit(chat),
            'referrals': referrals[0]['count'], 'referralUrl': self.link(f'r{uid}'),
            'addUrl': f'https://t.me/{self.username}?startgroup=nebula',
            'privateUrl': f'https://t.me/{self.username}',
        }, headers={'Cache-Control': 'no-store'})
