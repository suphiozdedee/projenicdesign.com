import os
import glob
import shutil
import sqlite3

user_data = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
history_files = glob.glob(os.path.join(user_data, "*", "History")) + [os.path.join(user_data, "Default", "History")]
temp_db = os.path.expandvars(r"%TEMP%\temp_chrome_history.db")

for hf in set(history_files):
    if not os.path.exists(hf):
        continue
    try:
        shutil.copyfile(hf, temp_db)
        conn = sqlite3.connect(temp_db)
        cur = conn.cursor()
        cur.execute("SELECT url, title, datetime(last_visit_time/1000000-11644473600, 'unixepoch', 'localtime') as visit_time FROM urls WHERE url LIKE '%hostinger%' ORDER BY last_visit_time DESC LIMIT 15")
        rows = cur.fetchall()
        for url, title, vtime in rows:
            print(f"[{vtime}] {title} -> {url}")
        conn.close()
        break
    except Exception as e:
        pass
