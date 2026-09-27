import sqlite3
import os
import shutil

src = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\Profile 2\Network\Cookies")
dst = os.path.expandvars(r"%TEMP%\temp_cookies.db")
shutil.copyfile(src, dst)
conn = sqlite3.connect(dst)
cur = conn.cursor()
cur.execute("SELECT host_key, name, value, length(encrypted_value) FROM cookies WHERE host_key LIKE '%hstgr.io%' OR host_key LIKE '%hostinger%'")
rows = cur.fetchall()
for r in rows:
    print(r)
conn.close()
