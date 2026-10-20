"""Synthetic benchmark: no game assets; does not measure Windows capture/OCR."""
from pathlib import Path
import statistics
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image,ImageDraw,ImageFont
from umacoach.templates import TemplateBank


def text_image(text):
    font=ImageFont.load_default(size=32);pic=Image.new('RGB',(130,54),'black')
    ImageDraw.Draw(pic).text((6,4),text,font=font,fill='white');return pic


def pattern(color,mark):
    pic=Image.new('RGB',(130,54),color);d=ImageDraw.Draw(pic)
    for i in range(mark+1):d.rectangle((8+i*9,6,11+i*9,45),fill='white')
    return pic


def fixture(bank):
    for c in '0123456789+':bank.add(text_image(c),'gain_speed',[0,0,1,1],c,sample_type='glyph')
    for i,name in enumerate(['速度','耐力','力量','毅力','智力']):bank.add(pattern(['red','blue','green','orange','purple'][i],i),'selected',[0,0,1,1],name,sample_type='icon')
    for i in range(1,4):bank.add(pattern(['black','navy','teal'][i-1],i),'year',[0,0,1,1],str(i),sample_type='icon')
    for i,name in enumerate(['前半','後半']):bank.add(pattern(['black','orange'][i],i+2),'half',[0,0,1,1],name,sample_type='icon')
    for i,name in enumerate(['blue','green','pink']):bank.add(pattern(['blue','green','magenta'][i],i),'fragment',[0,0,1,1],name,sample_type='icon')
    bank.add(pattern('cyan',1),'mood',[0,0,1,1],'好調',sample_type='icon')
    bank.add(pattern('black',0),'card0_avatar',[0,0,1,1],'empty',sample_type='icon')
    bank.add(pattern('blue',3),'card0_avatar',[0,0,1,1],'A',sample_type='icon')
    for part in ('friendship','bond','hint'):
        for name,color in [('yes','green'),('no','black')]:bank.add(pattern(color,2),f'card0_{part}',[0,0,1,1],name,sample_type='icon')
    tiles={f'gain_{k}':text_image(v) for k,v in zip(['speed','stamina','power','guts','wisdom','skill'],['+20','+0','+8','+0','+0','+35'])}
    tiles.update(failure=text_image('0'),month=text_image('5'),remaining=text_image('3'),selected=pattern('red',0),year=pattern('navy',2),half=pattern('black',2),fragment=pattern('magenta',2),mood=pattern('cyan',1))
    for slot in range(6):
        tiles[f'card{slot}_avatar']=pattern('blue',3) if slot<2 else pattern('black',0)
        for part in ('friendship','bond','hint'):
            yes=(slot==0 and part in ('friendship','hint')) or (slot==1 and part=='bond')
            tiles[f'card{slot}_{part}']=pattern('green' if yes else 'black',2)
    canvas=Image.new('RGB',(1400,1000),'#222222');regions={}
    for i,(field,pic) in enumerate(tiles.items()):
        x=(i%8)*170+10;y=(i//8)*85+10;canvas.paste(pic,(x,y))
        rect=[x/1400,y/1000,(x+130)/1400,(y+54)/1000]
        bank.add(canvas,field,rect,'',sample_type='region');regions[field]=rect
    return canvas,regions


def main():
    with tempfile.TemporaryDirectory() as tmp:
        bank=TemplateBank(Path(tmp)/'templates');image,_=fixture(bank)
        times=[]
        for _ in range(35):
            bank.cached={};start=time.perf_counter();result=bank.recognize(image);times.append((time.perf_counter()-start)*1000)
        assert result['values']['gain_speed']==20 and result['values']['gain_skill']==35
        assert result['values']['selected']=='速度'
        cold=sorted(times);cached=bank.recognize(image)
        print(f'Synthetic {len(result["values"])} regions, {len(bank.data["samples"])} templates: median {statistics.median(times):.2f} ms; p95 {cold[int(len(cold)*.95)]:.2f} ms; max {max(times):.2f} ms')
        print(f'Unchanged frame: {cached["metrics"]["elapsed_ms"]:.2f} ms; rematched fields {cached["metrics"]["processed_fields"]}')
        print('Measures Pillow template inference on this Linux host only; Windows capture, real client samples, and date OCR are not measured.')


if __name__=='__main__':main()
