"""Copy shop.db to ~/shopbot-backups safely while the bot is running; keep 14 days."""
import datetime
import os
import sqlite3
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
DB = os.environ.get("DB_PATH", "shop.db")
src_path = Path(DB) if os.path.isabs(DB) else BOT_DIR / DB
out_dir = Path.home() / "shopbot-backups"
out_dir.mkdir(exist_ok=True)

stamp = datetime.date.today().isoformat()
src = sqlite3.connect(src_path)
dst = sqlite3.connect(out_dir / f"shop-{stamp}.db")
src.backup(dst)
dst.close()
src.close()

for old in sorted(out_dir.glob("shop-*.db"))[:-14]:
    old.unlink()
