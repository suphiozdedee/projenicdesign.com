import os
import glob
import shutil
import sqlite3

user_data = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
history_files = glob.glob(os.path.join(user_data, "*", "History")) + [os.path.join(user_data, "Default", "History")]

temp_db = os.path.expandvars(r"%TEMP%\temp_chrome_history.db")

found = False
for hf in set(history_files):
    if not os.path.exists(hf):
        continue
    try:
        shutil.copyfile(hf, temp_db)
        conn = sqlite3.connect(temp_db)
        cur = conn.cursor()
        cur.execute("SELECT url, title FROM urls WHERE url LIKE '%hostinger%' ORDER BY id DESC LIMIT 40")
        rows = cur.fetchall()
        if rows:
            print(f"--- History from {hf} ---")
            for url, title in rows:
                print(f"{title} -> {url}")
            found = True
        conn.close()
    except Exception as e:
        # print(f"Error reading {hf}: {e}")
        pass

if not found:
    print("No Hostinger URLs found in Chrome history.")
