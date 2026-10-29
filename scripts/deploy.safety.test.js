import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';

const candidate=process.env.DEPLOY_SCRIPT_UNDER_TEST||'deploy.js';
const script=readFileSync(new URL(candidate,import.meta.url),'utf8');
const code=script.replace(/^import .*;\s*$/gm,'').replaceAll('import.meta.url',JSON.stringify('file:///fixture/project/scripts/deploy.js'));
const canary='FIXTURE_CREDENTIAL_ERROR_CANARY';

async function fixture({handshakeFailure=false,uploadFailure=false,closeFailure=false,envReadFailure=false,envFile=false,cli=false}={}){
 const calls=[],logs=[],remote=new Set(['index.html','waffle.html','waffle-gizlilik.html','waffle-kullanim-kosullari.html','assets/old.js']);
 class Client{
  constructor(){this.ftp={verbose:false};calls.push({type:'client',client:this});}
  async access(options){calls.push({type:'access',options});if(handshakeFailure)throw Error(canary);}
  async ensureDir(target){calls.push({type:'ensure',target});}
  async pwd(){return '/fixture-remote';}
  async cd(){calls.push({type:'cd'});}
  async list(dir){return dir==='assets'?[{name:'old.js',isDirectory:false}]:[...remote].filter(x=>!x.includes('/')).map(name=>({name,isDirectory:false})).concat([{name:'assets',isDirectory:true}]);}
  async remove(name){calls.push({type:'remove',name});remote.delete(name);}
  async removeEmptyDir(name){calls.push({type:'removeEmptyDir',name});}
  async uploadFromDir(local){calls.push({type:'upload',local});if(uploadFailure)throw Error(canary);remote.add('index.html');remote.add('assets/new.js');}
  close(){calls.push({type:'close'});if(closeFailure)throw Error(canary);}
 }
 const filesystem={
  existsSync:name=>name.endsWith('/dist')||name.endsWith('/.env')&&(envFile||envReadFailure),
  readFileSync(){if(envReadFailure)throw Error(canary);return 'FTP_SERVER=fixture-host\nFTP_USERNAME=fixture-user\nFTP_PASSWORD='+canary+'\nFTP_REMOTE_PATH=public_html\nFTP_PORT=21\n';},
  readdirSync:dir=>dir.endsWith('/assets')?[{name:'new.js',isDirectory:()=>false}]:['index.html','one.html','two.html','three.html','four.html'].map(name=>({name,isDirectory:()=>false})),
 };
 const env=envFile?{}:cli?{}:{FTP_SERVER:'fixture-host',FTP_USERNAME:'fixture-user',FTP_PASSWORD:canary,FTP_REMOTE_PATH:'public_html'};
 const controlledProcess={env,argv:cli?['node','deploy.js','fixture-host','fixture-user',canary,'public_html']:['node','deploy.js'],exitCode:0,exit(code){this.exitCode=code;calls.push({type:'exit',code});}};
 const context={ftp:{Client},fs:filesystem,path,fileURLToPath,process:controlledProcess,console:Object.fromEntries(['log','warn','error'].map(method=>[method,(...args)=>logs.push(args.map(String).join(' '))]))};
 await vm.runInNewContext(code,context,{timeout:1000,filename:candidate});
 return{calls,logs,remote,process:controlledProcess};
}

test('deployment requires explicit verified TLS with protocol logging disabled',async()=>{
 const f=await fixture();const access=f.calls.find(c=>c.type==='access');
 assert.equal(access.options.secure,true);assert.equal(access.options.secureOptions?.rejectUnauthorized,true);
 assert.equal(f.calls.find(c=>c.type==='client').client.ftp.verbose,false);
});

test('successful upload retains existing legal pages and old assets and always closes',async()=>{
 const f=await fixture();assert.equal(f.calls.filter(c=>c.type==='upload').length,1);
 for(const name of ['waffle.html','waffle-gizlilik.html','waffle-kullanim-kosullari.html','assets/old.js'])assert.equal(f.remote.has(name),true,name);
 assert.equal(f.calls.some(c=>c.type==='remove'||c.type==='removeEmptyDir'),false);assert.equal(f.calls.at(-1).type,'close');assert.equal(f.process.exitCode,0);
});

test('TLS handshake failure cannot upload, retry or expose raw errors and still closes',async()=>{
 const f=await fixture({handshakeFailure:true});assert.equal(f.calls.filter(c=>c.type==='access').length,1);assert.equal(f.calls.some(c=>c.type==='upload'),false);
 assert.equal(f.calls.at(-1).type,'close');assert.equal(f.calls.some(c=>c.type==='exit'),false);assert.equal(f.process.exitCode,1);assert.equal(f.logs.join('\n').includes(canary),false);
});

test('upload failure is reported safely without deletion, retries or immediate process exit',async()=>{
 const f=await fixture({uploadFailure:true});assert.equal(f.calls.filter(c=>c.type==='upload').length,1);assert.equal(f.calls.some(c=>c.type==='remove'||c.type==='removeEmptyDir'),false);
 assert.equal(f.calls.at(-1).type,'close');assert.equal(f.calls.some(c=>c.type==='exit'),false);assert.equal(f.process.exitCode,1);assert.equal(f.logs.join('\n').includes(canary),false);
});

test('normal env-file defaults and existing CLI configuration remain compatible without revealing credentials',async()=>{
 for(const options of [{envFile:true},{cli:true},{envReadFailure:true}]){
  const f=await fixture(options);const access=f.calls.find(c=>c.type==='access');assert.equal(access.options.host,'fixture-host');assert.equal(access.options.user,'fixture-user');assert.equal(access.options.password,canary);assert.equal(access.options.port,21);
  assert.equal(f.calls.find(c=>c.type==='ensure').target,'public_html');assert.equal(f.logs.join('\n').includes(canary),false);
 }
});

test('connection cleanup failure is bounded and cannot expose a raw transport error',async()=>{
 const f=await fixture({closeFailure:true});assert.equal(f.calls.filter(c=>c.type==='close').length,1);
 assert.equal(f.process.exitCode,1);assert.equal(f.logs.join('\n').includes(canary),false);
});
