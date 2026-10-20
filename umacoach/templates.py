"""Self-collected image templates. No game assets or external inference models."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid
from PIL import Image, ImageChops, ImageOps, ImageStat

ATTRS=['speed','stamina','power','guts','wisdom']
NUMERIC={**{f'gain_{k}':(0,300) for k in ATTRS+['skill']},'failure':(0,100),
    'month':(1,12),'remaining':(0,99),'fans':(0,99_999_999),**{f'stat_{k}':(0,2500) for k in ATTRS}}
ICONS={'selected':['速度','耐力','力量','毅力','智力'],
    'mood':['超絕好調','絕好調','好調','普通','不調','絕不調'],
    'year':['1','2','3'],'half':['前半','後半'],'fragment':['blue','green','pink'],
    'ailment':['yes','no']}
for slot in range(6):
    ICONS[f'card{slot}_avatar']=None
    for part in ('friendship','bond','hint'):ICONS[f'card{slot}_{part}']=['yes','no']
METERS={'energy':100,'guide_blue':8,'guide_green':8,'guide_pink':8}
FIELDS=list(NUMERIC)+list(ICONS)+list(METERS)+['date']


class TemplateError(ValueError):pass


def rect_valid(rect):
    return isinstance(rect,list) and len(rect)==4 and all(type(n) in (int,float) and math.isfinite(n) and 0<=n<=1 for n in rect) and rect[2]>rect[0] and rect[3]>rect[1]


def crop(image,rect):
    if not rect_valid(rect):raise TemplateError('选区坐标无效')
    box=[round(rect[i]*([image.width,image.height][i%2])) for i in range(4)]
    if box[2]-box[0]<2 or box[3]-box[1]<2:raise TemplateError('选区过小，请重新框选')
    return image.crop(box).convert('RGB')


def mask(pic,mode='auto'):
    pic=pic.convert('RGB')
    if pic.width>500 or pic.height>180:pic.thumbnail((500,180))
    if mode=='auto':
        # Estimate the local background from the border, not from a shipped palette.
        pixels=pic.load();border=[pixels[x,0] for x in range(pic.width)]+[pixels[x,pic.height-1] for x in range(pic.width)]
        bg=tuple(sorted(p[c] for p in border)[len(border)//2] for c in range(3))
        gray=ImageChops.difference(pic,Image.new('RGB',pic.size,bg)).convert('L')
        return gray.point(lambda n:255 if n>=35 else 0)
    gray=ImageOps.grayscale(pic)
    if mode=='dark':gray=ImageOps.invert(gray)
    return gray.point(lambda n:255 if n>=150 else 0)


def glyphs(pic,mode='auto'):
    binary=mask(pic,mode);runs=[];start=None
    for x in range(binary.width+1):
        occupied=x<binary.width and binary.crop((x,0,x+1,binary.height)).getbbox() is not None
        if occupied and start is None:start=x
        if not occupied and start is not None:
            tile=binary.crop((start,0,x,binary.height));box=tile.getbbox();start=None
            if box and box[3]-box[1]>=2 and sum(tile.histogram()[1:])>=2:runs.append(tile.crop(box))
    return runs


def normalize_glyph(pic):
    pic=pic.copy();pic.thumbnail((14,22),Image.Resampling.NEAREST)
    result=Image.new('L',(16,24));result.paste(pic,((16-pic.width)//2,(24-pic.height)//2))
    return result


def group(field):
    return field.split('_',1)[1] if re.fullmatch(r'card\d_(avatar|friendship|bond|hint)',field) else field


def rgb_signature(pic):return pic.convert('RGB').resize((24,24),Image.Resampling.BILINEAR)


def distance(a,b):
    return sum(ImageStat.Stat(ImageChops.difference(a,b)).mean)/(255*len(a.getbands()))


def color_at(pic,left,right):
    x1=max(0,min(pic.width-1,round(left*pic.width)));x2=max(x1+1,min(pic.width,round(right*pic.width)))
    area=pic.crop((x1,round(pic.height*.3),x2,max(1,round(pic.height*.7))))
    return tuple(round(n) for n in ImageStat.Stat(area).median)


def cdist(a,b):return sum(abs(x-y) for x,y in zip(a,b))/765


class TemplateBank:
    def __init__(self,directory):
        self.root=Path(directory);self.path=self.root/'index.json'
        self.data=dict(version=1,regions={},samples=[],meters={})
        self.images={};self.cached={};self.metrics={}
        self.overrides={}
        if self.path.exists():
            loaded=json.loads(self.path.read_text('utf-8'))
            if loaded.get('version')!=1:raise TemplateError('模板格式不兼容，请备份后重新采集')
            self.data=loaded
        self._load_images()

    def _load_images(self):
        self.images={}
        for s in self.data['samples']:
            if not re.fullmatch(r'[0-9a-f]{32}\.png',s.get('file','')):continue
            path=self.root/s['file']
            if path.exists():
                with Image.open(path) as pic:self.images[s['file']]=pic.copy()

    def save(self):
        self.root.mkdir(parents=True,exist_ok=True)
        temp=self.path.with_suffix('.tmp');temp.write_text(json.dumps(self.data,ensure_ascii=False,indent=2),'utf-8');temp.replace(self.path)
        self.cached={}

    def remove(self,filename):
        sample=next((s for s in self.data['samples'] if s['file']==filename),None)
        if not sample or not re.fullmatch(r'[0-9a-f]{32}\.png',filename):raise TemplateError('请选择已存在的样本')
        self.data['samples'].remove(sample);self.images.pop(filename,None)
        (self.root/filename).unlink(missing_ok=True);self.save()

    def override(self,image,field,value,expected_hash):
        if field not in self.data['regions'] or field=='date':raise TemplateError('该字段尚未配置选区；日期请用日期修正')
        pic=crop(image,self.data['regions'][field]['rect'])
        key=hashlib.blake2b(pic.resize((96,32)).tobytes(),digest_size=12).hexdigest()
        if key!=expected_hash:raise TemplateError('该区域已变化，请重新核对识别值')
        if value is not None:
            if field in NUMERIC or field in METERS:
                low,high=NUMERIC.get(field,(0,METERS.get(field)))
                if type(value) is not int or not low<=value<=high:raise TemplateError('修正数值超出范围')
            elif not isinstance(value,str) or not 1<=len(value)<=60 or (ICONS[field] and value not in ICONS[field]):raise TemplateError('修正图标标签无效')
        self.overrides[field]=dict(hash=key,value=value);self.cached.pop(field,None)

    def reset_regions(self):
        self.data['regions']={};self.save()

    def add(self,image,field,rect,label,mode='auto',sample_type='number'):
        if field not in FIELDS or mode not in ('auto','light','dark'):raise TemplateError('采样字段或前景模式无效')
        if sample_type not in ('number','glyph','icon','meter','region','zero'):raise TemplateError('采样类型无效')
        label=str(label).strip()
        pic=crop(image,rect)
        if len(self.data['samples'])>=800:raise TemplateError('模板已达800张，请先清理不再使用的样本')
        pending=[]
        if sample_type in ('number','glyph'):
            if not re.fullmatch(r'[0-9+%/,.]{1,12}',label):raise TemplateError('数字标签只接受0–9、+、%、/、逗号及小数点')
            if sample_type=='glyph' and len(label)!=1:raise TemplateError('单字模板一次只标一个字符')
            pieces=glyphs(pic,mode)
            if len(pieces)!=len(label):raise TemplateError(f'分割得到{len(pieces)}个字形，标签有{len(label)}个；请调整选区/前景，或逐字采集')
            if sample_type=='number' and field not in NUMERIC:raise TemplateError('请选择数字字段')
            pending=[('digits',char,normalize_glyph(tile)) for char,tile in zip(label,pieces)]
        elif sample_type=='zero':
            if not field.startswith('gain_') or label!='0':raise TemplateError('空白零收益只用于+收益字段，需亲自确认并填写0')
            pending=[(field+'_zero','0',pic.resize((96,32),Image.Resampling.BILINEAR))]
        elif sample_type=='icon':
            if field not in ICONS:raise TemplateError('请选择图标/头像字段')
            choices=ICONS[field]
            if not label or len(label)>60 or (choices and label not in choices):raise TemplateError('图标标签不在该字段允许值中')
            pending=[(group(field),label,rgb_signature(pic))]
        elif sample_type=='meter':
            if field not in METERS:raise TemplateError('请选择体力条或指南条')
            try:value=float(label)
            except ValueError:raise TemplateError('请填写当前百分比或指南格数') from None
            maximum=METERS[field]
            if not math.isfinite(value) or not 0<=value<=maximum:raise TemplateError('条形数值超出范围')
            ratio=value/maximum;meter=deepcopy(self.data['meters'].get(field,{}))
            if ratio>0:meter['filled']=color_at(pic,.02,max(.03,min(ratio*.6,.6)))
            if ratio<1:meter['empty']=color_at(pic,min(.98,ratio+(1-ratio)*.35),.99)
            if 'filled' in meter and 'empty' in meter and cdist(meter['filled'],meter['empty'])<.13:
                raise TemplateError('填充色和背景色太接近；请只框条形内部，避免文字/外框')
            for kind in ('filled','empty'):
                if kind in meter:
                    samples=meter.setdefault(kind+'_samples',[])
                    if not any(cdist(meter[kind],sample)<.02 for sample in samples):samples.append(meter[kind])
                    meter[kind+'_samples']=samples[-12:]
            self.data['meters'][field]=meter
        for category,value,tile in pending:
            filename=uuid.uuid4().hex+'.png';self.root.mkdir(parents=True,exist_ok=True);tile.save(self.root/filename)
            self.images[filename]=tile;self.data['samples'].append(dict(group=category,label=value,file=filename))
        if sample_type!='glyph':self.data['regions'][field]=dict(rect=rect,mode=mode)
        self.save()
        return self.status()

    def status(self):
        digits={s['label'] for s in self.data['samples'] if s['group']=='digits' and s['file'] in self.images}
        return dict(sample_count=len(self.data['samples']),missing_digits=[s for s in '0123456789+' if s not in digits],
            fields=sorted(self.data['regions']),meters=deepcopy(self.data['meters']),metrics=deepcopy(self.metrics),
            notice='模板来自本机采集。缺少/不明确的值为未知；相似度不是正确概率。建议窗口缩放变化后核对选区与识别。')

    def _match(self,pic,category,limit,margin):
        scores={}
        for sample in self.data['samples']:
            if sample['group']!=category or sample['file'] not in self.images:continue
            d=distance(pic,self.images[sample['file']]);label=sample['label']
            scores[label]=min(scores.get(label,1),d)
        ordered=sorted(scores.items(),key=lambda x:x[1])
        if not ordered:return None,None
        value,best=ordered[0]
        if best>limit or (len(ordered)>1 and ordered[1][1]-best<margin):return None,round(1-best,3)
        return value,round(1-best,3)

    def _number(self,pic,field,mode):
        # Only an explicitly labelled self sample can turn a blank gain into 0.
        # Use a strict per-pixel bound: small new digits must not average away.
        if field.startswith('gain_'):
            signature=pic.resize((96,32),Image.Resampling.BILINEAR)
            for sample in self.data['samples']:
                if sample['group']==field+'_zero' and sample['file'] in self.images:
                    diff=ImageChops.difference(signature,self.images[sample['file']])
                    if max(high for low,high in diff.getextrema())<=8:return 0,None
        digits={s['label'] for s in self.data['samples'] if s['group']=='digits' and s['file'] in self.images}
        if set('0123456789')-digits:return None,None
        pieces=glyphs(pic,mode)
        if not 1<=len(pieces)<=12:return None,None
        text='';scores=[]
        for piece in pieces:
            char,score=self._match(normalize_glyph(piece),'digits',.22,.025)
            if char is None:return None,score
            text+=char;scores.append(score)
        if not re.fullmatch(r'\+?\d+(?:,\d{3})*%?',text):return None,min(scores)
        if field.startswith('gain_') and not text.startswith('+') and text!='0':return None,min(scores)
        value=int(text.lstrip('+').rstrip('%').replace(',',''))
        low,high=NUMERIC[field]
        return (value if low<=value<=high else None),min(scores)

    def _meter(self,pic,field):
        meter=self.data['meters'].get(field,{})
        if not {'filled','empty'}<=set(meter):return None,None
        bins=100 if field=='energy' else 8
        states=[]
        for i in range(bins):
            c=color_at(pic,(i+.2)/bins,(i+.8)/bins)
            a=min(cdist(c,s) for s in meter.get('filled_samples',[meter['filled']]))
            b=min(cdist(c,s) for s in meter.get('empty_samples',[meter['empty']]))
            states.append(None if min(a,b)>.14 else 1 if a+.035<b else 0 if b+.035<a else None)
        if None in states:return None,None
        # A left-to-right fill must not contain a later filled segment after empty.
        if states!=sorted(states,reverse=True):return None,None
        return sum(states),None

    def recognize(self,image):
        start=time.perf_counter();values={};quality={};hashes={};processed=0
        for field,setup in self.data['regions'].items():
            if field not in FIELDS or field=='date':continue
            pic=crop(image,setup['rect']);key=hashlib.blake2b(pic.resize((96,32)).tobytes(),digest_size=12).hexdigest()
            old=self.cached.get(field)
            override=self.overrides.get(field)
            if override and override['hash']==key:value,score=override['value'],None
            elif old and old['hash']==key:value,score=old['result']
            else:
                processed+=1
                if field in NUMERIC:value,score=self._number(pic,field,setup['mode'])
                elif field in METERS:value,score=self._meter(pic,field)
                else:
                    choices=ICONS[field];known={s['label'] for s in self.data['samples'] if s['group']==group(field) and s['file'] in self.images}
                    ready=(not choices or field=='mood' or set(choices)<=known)
                    if group(field)=='avatar' and 'empty' not in known:ready=False
                    value,score=self._match(rgb_signature(pic),group(field),.12,.025) if ready else (None,None)
                self.cached[field]=dict(hash=key,result=(value,score))
            values[field]=value;quality[field]=score
            hashes[field]=key
        elapsed=(time.perf_counter()-start)*1000
        self.metrics=dict(elapsed_ms=round(elapsed,2),processed_fields=processed,configured_fields=len(values),over_budget=elapsed>300)
        return dict(values=values,quality=quality,field_hashes=hashes,metrics=deepcopy(self.metrics))
