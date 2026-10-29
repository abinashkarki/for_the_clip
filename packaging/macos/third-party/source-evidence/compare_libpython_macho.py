import pathlib,struct,hashlib,json
paths={'release':'/tmp/pbs-libpython3.12.dylib','toolchain':'output/macos-spike/toolchains/cpython-3.12.12-macos-aarch64-none/lib/libpython3.12.dylib','app':'output/macos-spike/Clippie.app/Contents/Resources/python/lib/libpython3.12.dylib'}
def parse(path):
 b=pathlib.Path(path).read_bytes(); magic,_,_,_,ncmds,_,_,_=struct.unpack_from('<8I',b,0);assert magic==0xfeedfacf
 off=32; sections={}; cmds=[]
 for i in range(ncmds):
  cmd,size=struct.unpack_from('<II',b,off); raw=b[off:off+size];entry={'cmd':hex(cmd),'size':size}
  if cmd==0x19:
   n=struct.unpack_from('<I',b,off+64)[0]
   for j in range(n):
    v=struct.unpack_from('<16s16sQQIIIIIIII',b,off+72+80*j)
    sn=v[0].split(b'\0')[0].decode(); seg=v[1].split(b'\0')[0].decode(); sectsize=v[3]; fileoff=v[4]; flags=v[8]
    if flags&255 in (1,12,18):continue
    if fileoff+sectsize > len(b):raise ValueError('section outside file')
    sections[seg+','+sn]={'size':sectsize,'sha256':hashlib.sha256(b[fileoff:fileoff+sectsize]).hexdigest()}
  if cmd in (0xd,0xc,0x80000018,0x8000001f,0x80000023):
   pos=struct.unpack_from('<I',b,off+8)[0];entry['name']=raw[pos:].split(b'\0')[0].decode(errors='replace')
  elif cmd==0x1b:entry['uuid']=raw[8:24].hex()
  elif cmd==0x1d:entry['dataoff'],entry['datasize']=struct.unpack_from('<II',b,off+8)
  cmds.append(entry);off+=size
 return {'path':str(path),'file_sha256':hashlib.sha256(b).hexdigest(),'sections':sections,'commands':cmds}
r={k:parse(p) for k,p in paths.items()}; r['all_section_payloads_identical']=r['release']['sections']==r['toolchain']['sections']==r['app']['sections'];r['qualification']='Section payload hashes exclude Mach-O headers, load commands, signature and unsectioned data; identical section hashes do not prove complete binary identity.'
p='packaging/macos/third-party/standalone-python/libpython-macho-evidence.json';open(p,'w').write(json.dumps(r,indent=2)+'\n')
print('equal',r['all_section_payloads_identical'],'section count',len(r['release']['sections']))
for k in paths:
 print(k,[(c['cmd'],c.get('name'),c.get('dataoff'),c.get('datasize')) for c in r[k]['commands'] if c['cmd'] in ('0xd','0x1d')])
print('differences',[(k, r['release']['sections'].get(k),r['toolchain']['sections'].get(k),r['app']['sections'].get(k)) for k in set(r['release']['sections'])|set(r['toolchain']['sections'])|set(r['app']['sections']) if not(r['release']['sections'].get(k)==r['toolchain']['sections'].get(k)==r['app']['sections'].get(k))][:10])
