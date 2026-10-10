#!/usr/bin/env python3
"""Repack the user's boot v1 and update its existing unsigned AVB hash footer.
Strictly handles original XCover4s boot image; never attempts generic signing.
"""
from pathlib import Path
import gzip
import hashlib
import io
import os
import struct
import subprocess
import tempfile

PARTITION=37748736
ORIGINAL_SHA='f94927e36475b2c6ba7b2c1925c3a5969d8d42b321ffc9309aa653cddc7a99e9'
original=Path('boot-original.img').read_bytes()
if len(original)!=PARTITION or hashlib.sha256(original).hexdigest()!=ORIGINAL_SHA:
    raise SystemExit('Wrong boot-original.img; refusing to package')
if original[:8]!=b'ANDROID!':raise SystemExit('Invalid boot magic')
ksize,ka,rsize,ra,ssize,sa,taddr,page,ver,osver=struct.unpack_from('<10I',original,8)
if page!=2048 or ver!=1 or ssize!=0:raise SystemExit('Unknown boot format')
roundp=lambda n:(n+page-1)//page*page
kernel=Path('out/arch/arm64/boot/Image').read_bytes()
if not (18_000_000<len(kernel)<30_000_000):raise SystemExit('Unexpected kernel image size')
ram_off=page+roundp(ksize)
old_ram=original[ram_off:ram_off+rsize]
if old_ram[:2]!=b'\x1f\x8b':raise SystemExit('Unknown ramdisk compression')
initramfs=gzip.decompress(old_ram)
if not initramfs.startswith(b'070701'):raise SystemExit('Ramdisk not newc cpio')
with tempfile.TemporaryDirectory() as temp:
    stage=Path(temp)
    subprocess.run(['cpio','-idm','--quiet','--no-absolute-filenames','--no-preserve-owner'],input=initramfs,cwd=stage,check=True)
    init=stage/'init'
    if not init.is_file() or not initramfs.startswith(b'070701'):raise SystemExit('Missing original /init')
    init.rename(stage/'init.original')
    for path,name in [('tools/init-wrapper.arm64','init'),('tools/rootbroker.arm64','rootbroker-v64')]:
        dst=stage/name
        dst.write_bytes(Path(path).read_bytes())
        dst.chmod(0o755)
    # Require a static launcher and broker: they run before Android linker is mounted.
    for name in ('init','rootbroker-v64'):
        elf=(stage/name).read_bytes()
        if elf[:4]!=b'\x7fELF':raise SystemExit('Not ELF: '+name)
    # cpio --owner=0:0 preserves correct init boot root ownership after runner extraction.
    listing=subprocess.check_output(['find','.','-print0'],cwd=stage)
    cp=subprocess.run(['cpio','--null','-o','--format=newc','--owner=0:0','--quiet'],cwd=stage,input=listing,stdout=subprocess.PIPE,check=True)
    ram=gzip.compress(cp.stdout,compresslevel=9,mtime=0)
header=bytearray(original[:page])
struct.pack_into('<I',header,8,len(kernel))
struct.pack_into('<I',header,16,len(ram))
cmd=header[64:576].split(b'\0',1)[0]
for arg in (b'enforcing=0',b'androidboot.selinux=permissive'):
    if arg not in cmd:cmd+=b' '+arg
if len(cmd)>511:raise SystemExit('Kernel cmdline too long')
header[64:576]=cmd.ljust(512,b'\0')
# bootimg conventional SHA1: kernel+size, ramdisk+size, second size, recovery_dtbo size.
h=hashlib.sha1()
for blob in (kernel,ram,b'',b''):
    h.update(blob)
    h.update(struct.pack('<I',len(blob)))
header[576:608]=h.digest().ljust(32,b'\0')
payload=bytes(header)+kernel.ljust(roundp(len(kernel)),b'\0')+ram.ljust(roundp(len(ram)),b'\0')
# Keep the exact original unsigned vbmeta properties and extra descriptors.
magic,major,minor,old_data,old_meta,metasz=struct.unpack('>4sIIQQQ',original[-64:-28])
if magic!=b'AVBf' or major!=1:raise SystemExit('Unsupported AVB footer')
base=original[old_meta:old_meta+metasz]
if base[:4]!=b'AVB0' or struct.unpack_from('>I',base,28)[0]!=0:
    raise SystemExit('Original vbmeta must have algorithm NONE')
if len(base)!=704:raise SystemExit('Unexpected vbmeta length')
meta=bytearray(base)
pos=256+struct.unpack_from('>Q',meta,96)[0]
end=pos+struct.unpack_from('>Q',meta,104)[0]
seen=False
while pos<end:
    tag,sz=struct.unpack_from('>QQ',meta,pos)
    if sz%8 or pos+16+sz>end:raise SystemExit('Invalid AVB descriptor')
    if tag==2:
        if seen:raise SystemExit('Multiple hash descriptors not supported')
        dsize=struct.unpack_from('>Q',meta,pos+16)[0]
        nm,saltlen,diglen,flags=struct.unpack_from('>IIII',meta,pos+56)
        off=pos+16+8+32+16+60
        part=bytes(meta[off:off+nm]);salt=bytes(meta[off+nm:off+nm+saltlen]);digest=bytes(meta[off+nm+saltlen:off+nm+saltlen+diglen])
        if part!=b'boot' or diglen!=32 or meta[pos+24:pos+30]!=b'sha256':raise SystemExit('Unexpected AVB hash descriptor')
        if hashlib.sha256(salt+original[:dsize]).digest()!=digest:raise SystemExit('Original AVB digest mismatch')
        desc=pos
        seen=True
    pos+=16+sz
if not seen:raise SystemExit('No hash descriptor found')
# AVB hash footer image_size uses 4096 aligned boot data, not raw ELF size.
padded=(len(payload)+4095)//4096*4096
if padded+len(meta)+64>=PARTITION:raise SystemExit('Boot image is too large for partition')
boots=payload.ljust(padded,b'\0')
struct.pack_into('>Q',meta,desc+16,padded)
meta[off+nm+saltlen:off+nm+saltlen+diglen]=hashlib.sha256(salt+boots).digest()
footer=bytearray(original[-64:]);struct.pack_into('>QQQ',footer,12,padded,padded,len(meta))
newimage=(boots+meta).ljust(PARTITION-64,b'\0')+footer
assert len(newimage)==PARTITION
# Full reparse and roundtrip check of unsigned AVB hash descriptor.
assert hashlib.sha256(salt+newimage[:padded]).digest()==newimage[padded+off+nm+saltlen:padded+off+nm+saltlen+diglen]
assert gzip.decompress(ram)[:6]==b'070701'
Path('deliver').mkdir(exist_ok=True)
Path('deliver/boot.img').write_bytes(newimage)
Path('deliver/V64-image-sha256.txt').write_text(hashlib.sha256(newimage).hexdigest()+'  boot.img\n')
print('V64 boot.img created:',len(newimage),'bytes; kernel:',len(kernel),'ramdisk:',len(ram),'AVB:',padded,'verified')
