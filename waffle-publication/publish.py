"""Upload only the three approved public Waffle notices using verified FTPS."""
import ftplib, hashlib, io, json, os, pathlib, ssl, sys, socket, subprocess, re
PAGES=('waffle.html','waffle-gizlilik.html','waffle-kullanim-kosullari.html')
base=pathlib.Path(__file__).resolve().parent/'pages'
context=ssl.create_default_context();context.minimum_version=ssl.TLSVersion.TLSv1_2
stage='LOCAL_FILES'
try:
    content={name:(base/name).read_bytes() for name in PAGES}
    assert all(b'<html lang="tr"' in b and b'Waffle Free Research' in b and b'<script' not in b for b in content.values())
    host=os.environ['FTP_SERVER'];user=os.environ['FTP_USERNAME'];password=os.environ['FTP_PASSWORD']
    assert host and user and password
    stage='TLS_LOGIN'
    with ftplib.FTP_TLS(context=context,timeout=30) as client:
        client.connect(host,21)
        try:
            client.auth()
        except ssl.SSLCertVerificationError as cert_error:
            if cert_error.verify_code != 62:
                raise
            # No credentials have been transmitted. Resolve this SAME server's
            # provider hostname and validate its real certificate normally.
            original_ips=set(socket.gethostbyname_ex(host)[2])
            aliases=set()
            for address in original_ips:
                try: aliases.add(socket.gethostbyaddr(address)[0].rstrip('.'))
                except socket.herror: pass
            # Inspect only the public certificate, without a USER/PASS command.
            # This diagnostic is never an authenticated transfer connection.
            if not aliases:
                stage='PUBLIC_CERTIFICATE_NAME'
                raw=subprocess.run(['openssl','s_client','-connect',host+':21','-starttls','ftp','-servername',host,'-showcerts','-verify_return_error'],input=b'',stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=12)
                pem=re.search(rb'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----',raw.stdout,re.S)
                if raw.returncode!=0 or pem is None: raise RuntimeError('certificate_chain_unverified')
                certificate=subprocess.run(['openssl','x509','-noout','-ext','subjectAltName'],input=pem.group(0),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=3)
                aliases.update(re.findall(r'DNS:([A-Za-z0-9.-]+)',certificate.stdout.decode('ascii','ignore')))
            candidates=[]
            for name in aliases:
                if not name.endswith(('.hstgr.io','.main-hosting.eu','.hostinger.com')): continue
                try:
                    if set(socket.gethostbyname_ex(name)[2]) & original_ips:candidates.append(name)
                except socket.gaierror: pass
            if len(candidates)!=1:
                raise
            stage='VERIFIED_PROVIDER_HOSTNAME'
            client.close();client.connect(candidates[0],21);client.auth()
        client.login(user,password);client.prot_p()
        # No arbitrary paths, mirroring, directory cleanup, or remote delete.
        stage='UPLOAD_VERIFY'
        for name,body in content.items():
            client.storbinary('STOR '+name,io.BytesIO(body))
            received=io.BytesIO();client.retrbinary('RETR '+name,received.write)
            assert hashlib.sha256(body).digest()==hashlib.sha256(received.getvalue()).digest()
            print(json.dumps({'path':name,'sha256':hashlib.sha256(body).hexdigest(),'verified':True}))
except Exception as error:
    # Never print credential-bearing FTP errors, server details, or env values.
    code='TLS_CERTIFICATE_ERROR' if isinstance(error,ssl.SSLCertVerificationError) else 'TLS_ERROR' if isinstance(error,ssl.SSLError) else 'DNS_ERROR' if isinstance(error,(socket.gaierror,socket.herror)) else 'FTP_PERMISSION_ERROR' if isinstance(error,ftplib.error_perm) else 'MISSING_CONFIGURATION' if isinstance(error,(KeyError,AssertionError)) else 'NETWORK_OR_REMOTE_ERROR'
    print(json.dumps({'publication':'failed','stage':stage,'code':code,'certificateCode':getattr(error,'verify_code',None)}),file=sys.stderr)
    sys.exit(1)
