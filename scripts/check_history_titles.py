import os
import glob
import shutil
import sqlite3

user_data = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
history_files = glob.glob(os.path.join(user_data, "*", "History")) + [os.path.join(user_data, "Default", "History")]
temp_db = os.path.expandvars(r"%TEMP%\temp_chrome_history_mail.db")

for hf in set(history_files):
    if not os.path.exists(hf):
        continue
    try:
        shutil.copyfile(hf, temp_db)
        conn = sqlite3.connect(temp_db)
        cur = conn.cursor()
        cur.execute("SELECT url, title FROM urls WHERE url LIKE '%mail.google.com%' OR title LIKE '%Hostinger%' OR url LIKE '%hostinger%' ORDER BY id DESC LIMIT 25")
        rows = cur.fetchall()
        if rows:
            for r in rows:
                print(r[1], "-->", r[0])
            conn.close()
            break
        conn.close()
    except Exception as e:
        pass
