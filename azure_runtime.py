"""Azure-only runtime primitives; legacy deployment remains unchanged."""
import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager

def enabled():
    return os.getenv("NEBULA_RUNTIME", "").lower() == "azure"

class RuntimeSettings:
    DEFAULTS = {"audio": (20, 40), "text": (20, 50)}
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.execute("CREATE TABLE IF NOT EXISTS azure_slots (kind TEXT PRIMARY KEY, slots INTEGER NOT NULL)")
        for kind, (default, _) in self.DEFAULTS.items():
            db.execute("INSERT OR IGNORE INTO azure_slots VALUES (?, ?)", (kind, default))
        db.commit()

    def get(self, kind):
        return self.db.execute("SELECT slots FROM azure_slots WHERE kind=?", (kind,)).fetchone()[0]

    def set(self, kind, slots):
        if kind not in self.DEFAULTS or type(slots) is not int or not 1 <= slots <= self.DEFAULTS[kind][1]:
            raise ValueError("Invalid Azure slot limit")
        self.db.execute("UPDATE azure_slots SET slots=? WHERE kind=?", (slots, kind))
        self.db.commit()

class SlotGate:
    def __init__(self, settings, kind):
        self.settings = settings
        self.kind = kind
        self.active = 0
        self.waiting = 0
        self.condition = asyncio.Condition()

    @asynccontextmanager
    async def slot(self):
        async with self.condition:
            self.waiting += 1
            try:
                await self.condition.wait_for(lambda: self.active < self.settings.get(self.kind))
                self.active += 1
            finally:
                self.waiting -= 1
        try:
            yield
        finally:
            async with self.condition:
                self.active -= 1
                self.condition.notify_all()

    async def resize(self, slots):
        async with self.condition:
            self.settings.set(self.kind, slots)
            self.condition.notify_all()
