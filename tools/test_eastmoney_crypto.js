/* Offline interoperability check: the vendored official JSEncrypt -> OpenSSL. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const vm = require('node:vm');
const crypto = require('node:crypto');
const {spawnSync} = require('node:child_process');
const sandbox = {crypto: crypto.webcrypto, navigator:{appName:'Netscape'}, console, addEventListener(){}};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../chaogu/web/vendor/jsencrypt-eastmoney.js'),'utf8'),sandbox);
const pair = crypto.generateKeyPairSync('rsa',{modulusLength:1024, publicKeyEncoding:{type:'spki',format:'pem'},privateKeyEncoding:{type:'pkcs8',format:'pem'}});
const rsa = new sandbox.JSEncrypt(); rsa.setPublicKey(pair.publicKey);
const first = rsa.encrypt('fixture-password');
const second = rsa.encrypt('fixture-password');
assert.notEqual(first,second,'PKCS#1 padding must be randomized');
assert.equal(Buffer.from(first,'base64').length,128);
const dir = fs.mkdtempSync(path.join(os.tmpdir(),'chaogu-crypto-'));
try {
  const keyPath = path.join(dir,'test-only-key.pem');
  fs.writeFileSync(keyPath,pair.privateKey,{mode:0o600});
  const result = spawnSync('openssl',['pkeyutl','-decrypt','-inkey',keyPath,'-pkeyopt','rsa_padding_mode:pkcs1'],{input:Buffer.from(first,'base64')});
  assert.equal(result.status,0,result.stderr.toString());
  assert.equal(result.stdout.toString(),'fixture-password');
} finally { fs.rmSync(dir,{recursive:true,force:true}); }
console.log('Official JSEncrypt encryption verified against OpenSSL; randomized padding verified.');
