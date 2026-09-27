import os, json, base64, sqlite3, shutil, glob
import ctypes
from ctypes import wintypes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_byte))]

def decrypt_dpapi(data):
    blob_in = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_byte)))
    blob_out = DATA_BLOB()
    if ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        res = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return res
    return None

local_state_path = os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\User Data\Local State')
with open(local_state_path, 'r', encoding='utf-8') as f:
    local_state = json.load(f)

encrypted_key = base64.b64decode(local_state['os_crypt']['encrypted_key'])
aes_key = decrypt_dpapi(encrypted_key[5:])
aes = AESGCM(aes_key)

def decrypt_val(encrypted_value):
    try:
        if encrypted_value[:3] in (b'v10', b'v11', b'v20'):
            nonce = encrypted_value[3:15]
            ciphertext = encrypted_value[15:]
            return aes.decrypt(nonce, ciphertext, None).decode('utf-8', errors='ignore')
        else:
            return decrypt_dpapi(encrypted_value).decode('utf-8', errors='ignore')
    except Exception as e:
        import traceback
        return f"<error: {e} | {traceback.format_exc()}>"

profiles = glob.glob(os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\User Data\Profile*')) + [os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\User Data\Default')]

found_cookies = []

for p in profiles:
    cookie_file = os.path.join(p, 'Network', 'Cookies')
    if os.path.exists(cookie_file):
        tmp = os.path.expandvars(r'%TEMP%\temp_c.db')
        try:
            shutil.copyfile(cookie_file, tmp)
            conn = sqlite3.connect(tmp)
            cur = conn.cursor()
            cur.execute("SELECT host_key, name, encrypted_value FROM cookies WHERE host_key LIKE '%hstgr%' OR host_key LIKE '%hostinger%'")
            rows = cur.fetchall()
            if rows:
                print(f"Profile {os.path.basename(p)}: {len(rows)} matching cookies")
                for host, name, val in rows:
                    dec = decrypt_val(val)
                    print(f"  [{os.path.basename(p)}] {host} | {name} = {dec[:20]}... (len {len(dec)})")
                    found_cookies.append({'profile': os.path.basename(p), 'host': host, 'name': name, 'value': dec})
            conn.close()
        except Exception as ex:
            print(f"Profile {os.path.basename(p)} error: {ex}")

with open(r'c:\Users\Projenic_Design\Desktop\projenicdesign.com\scripts\hostinger_cookies.json', 'w', encoding='utf-8') as f:
    json.dump(found_cookies, f, indent=2)
print(f"Saved {len(found_cookies)} cookies to hostinger_cookies.json")
