"""Upload only the three approved public Waffle notices using verified FTPS."""
import ftplib, hashlib, io, json, os, pathlib, ssl, sys
PAGES=('waffle.html','waffle-gizlilik.html','waffle-kullanim-kosullari.html')
base=pathlib.Path(__file__).resolve().parent/'pages'
context=ssl.create_default_context();context.minimum_version=ssl.TLSVersion.TLSv1_2
try:
    content={name:(base/name).read_bytes() for name in PAGES}
    assert all(b'<html lang="tr"' in b and b'Waffle Free Research' in b and b'<script' not in b for b in content.values())
    host=os.environ['FTP_SERVER'];user=os.environ['FTP_USERNAME'];password=os.environ['FTP_PASSWORD']
    with ftplib.FTP_TLS(context=context,timeout=30) as client:
        client.connect(host,21);client.login(user,password);client.prot_p()
        # No arbitrary paths, mirroring, directory cleanup, or remote delete.
        for name,body in content.items():
            client.storbinary('STOR '+name,io.BytesIO(body))
            received=io.BytesIO();client.retrbinary('RETR '+name,received.write)
            assert hashlib.sha256(body).digest()==hashlib.sha256(received.getvalue()).digest()
            print(json.dumps({'path':name,'sha256':hashlib.sha256(body).hexdigest(),'verified':True}))
except Exception:
    # Never print credential-bearing FTP errors, server details, or env values.
    print('Waffle publication failed; no unencrypted fallback or automatic retry.',file=sys.stderr)
    sys.exit(1)
