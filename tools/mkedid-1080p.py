import struct,sys
e=bytearray(128)
e[0:8]=b'\x00\xff\xff\xff\xff\xff\xff\x00'
# manufacturer "MHS"
m=((ord('M')-64)<<10)|((ord('H')-64)<<5)|(ord('S')-64); e[8:10]=struct.pack('>H',m)
e[10:12]=b'\x01\x00'; e[12:16]=b'\x01\x00\x00\x00'; e[16]=1; e[17]=34  # week1 2024
e[18]=1; e[19]=3
e[20]=0x80  # digital
e[21]=52; e[22]=29  # cm
e[23]=120; e[24]=0x0A  # gamma 2.2, RGB, preferred timing in DTD1
e[25:35]=bytes([0xEE,0x91,0xA3,0x54,0x4C,0x99,0x26,0x0F,0x50,0x54])
e[35:38]=b'\x00\x00\x00'
for i in range(38,54,2): e[i]=1; e[i+1]=1
def dtd(pclk,ha,hb,va,vb,hso,hsw,vso,vsw,hmm,vmm):
    d=bytearray(18); struct.pack_into('<H',d,0,pclk//10000)
    d[2]=ha&0xff; d[3]=hb&0xff; d[4]=((ha>>8)<<4)|(hb>>8)
    d[5]=va&0xff; d[6]=vb&0xff; d[7]=((va>>8)<<4)|(vb>>8)
    d[8]=hso&0xff; d[9]=hsw&0xff; d[10]=((vso&0xf)<<4)|(vsw&0xf)
    d[11]=((hso>>8)<<6)|((hsw>>8)<<4)|((vso>>4)<<2)|(vsw>>4)
    d[12]=hmm&0xff; d[13]=vmm&0xff; d[14]=((hmm>>8)<<4)|(vmm>>8); d[17]=0x1E
    return d
e[54:72]=dtd(148500000,1920,280,1080,45,88,44,4,5,521,293)
r=bytearray(18); r[3]=0xFD; r[5:10]=bytes([50,75,30,80,16]); r[10]=0; r[11:18]=b'\x0a'+b'\x20'*6
e[72:90]=r
n=bytearray(18); n[3]=0xFC; nm=b'MH-1080P\n'; n[5:5+len(nm)]=nm; n[5+len(nm):18]=b'\x20'*(13-len(nm)); e[90:108]=n
d=bytearray(18); d[3]=0x10; e[108:126]=d
e[126]=0; e[127]=(-sum(e[:127]))&0xff
open(sys.argv[1],'wb').write(e)
