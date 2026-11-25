"""Shared runtime lease; all polling hosts must participate before activation."""
import asyncio
import uuid

class RuntimeLease:
    def __init__(self, query, session, role):
        self.query, self.session, self.role = query, session, role
        self.owner = role + ':' + uuid.uuid4().hex

    async def initialize(self):
        await self.query(self.session,'CREATE TABLE IF NOT EXISTS runtime_preference (id INTEGER PRIMARY KEY, azure_until INTEGER NOT NULL)')
        await self.query(self.session, "CREATE TABLE IF NOT EXISTS runtime_lease (id INTEGER PRIMARY KEY, owner TEXT NOT NULL, role TEXT NOT NULL, expires INTEGER NOT NULL)")

    async def acquire(self):
        if self.role=='azure':
            await self.query(self.session,"INSERT INTO runtime_preference VALUES (1,unixepoch()+30) ON CONFLICT(id) DO UPDATE SET azure_until=excluded.azure_until")
        else:
            rows=await self.query(self.session,"SELECT azure_until FROM runtime_preference WHERE id=1 AND azure_until>unixepoch()")
            if rows: return False
        return await self._acquire()

    async def _acquire(self):
        rows = await self.query(self.session, """INSERT INTO runtime_lease VALUES (1,?,?,unixepoch()+90)
        ON CONFLICT(id) DO UPDATE SET owner=excluded.owner, role=excluded.role, expires=excluded.expires
        WHERE runtime_lease.expires<=unixepoch() OR runtime_lease.owner=excluded.owner
        RETURNING owner""", (self.owner,self.role))
        return bool(rows and rows[0]['owner']==self.owner)

    async def renew(self):
        if self.role=='azure':
            await self.query(self.session,"INSERT INTO runtime_preference VALUES (1,unixepoch()+30) ON CONFLICT(id) DO UPDATE SET azure_until=excluded.azure_until")
        else:
            rows=await self.query(self.session,"SELECT azure_until FROM runtime_preference WHERE id=1 AND azure_until>unixepoch()")
            if rows: return False
        rows = await self.query(self.session, """UPDATE runtime_lease SET expires=unixepoch()+90
        WHERE id=1 AND owner=? AND expires>unixepoch() RETURNING owner""", (self.owner,))
        return bool(rows and rows[0]['owner']==self.owner)

    async def release(self):
        await self.query(self.session, "DELETE FROM runtime_lease WHERE id=1 AND owner=?", (self.owner,))

    async def monitor(self, on_lost):
        while True:
            await asyncio.sleep(15)
            try:
                valid=await asyncio.wait_for(self.renew(),timeout=10)
            except Exception:
                valid=False
            if not valid:
                await on_lost()
                return
