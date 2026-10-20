"""No-sampling local reader: OCR anchors locate their own regions on screen.

Stable observations enter the run. Pixel equality caches OCR output, not invented
labels. No game assets, external models, mouse control or memory access.
"""
from copy import deepcopy
import hashlib
import json
import re
import threading
import time
from .local_ocr import LABELS,TYPES,OcrError,clean,expanded_words,parse_date,parse_number,selected_type,parse_result
from .templates import crop,glyphs,rect_valid

TW_NAMES={'贵妇人':'貴婦人','黄金巨匠':'黃金巨匠','无声铃鹿':'無聲鈴鹿','特别周':'特別週','东海帝王':'東海帝皇','目白麦昆':'目白麥昆'}


def character_from_header(rows,image):
    from .schedule import catalog
    aliases={}
    for profile in catalog()['profiles']:
        name=profile['name']
        for alias in {name,profile.get('name_jp',''),TW_NAMES.get(name,name)}:
            if alias:aliases[clean(alias)]=name
    candidates={}
    for row in rows:
        if min(w['y'] for w in row['words'])>image.height*.35:continue
        text=re.sub(r'^(?:養成馬娘|育成ウマ娘|角色)[:：]?','',row['text'])
        if text in aliases:candidates[aliases[text]]=text
    return next(iter(candidates.values())) if len(candidates)==1 else None


def fingerprint(image,rect):
    pic=crop(image,rect)
    return hashlib.blake2b(pic.tobytes(),digest_size=16).hexdigest()


def bounds(words,image,pad=2):
    return [round(v,6) for v in [max(0,(min(w['x'] for w in words)-pad)/image.width),
            max(0,(min(w['y'] for w in words)-pad)/image.height),
            min(1,(max(w['x']+w['w'] for w in words)+pad)/image.width),
            min(1,(max(w['y']+w['h'] for w in words)+pad)/image.height)]]


def mapped_words(image,raw,regions):
    """Map each OCR crop/scale back to the captured client coordinate system."""
    result=[]
    for item in raw.get('items',[]):
        rect=regions.get(item.get('key'))
        if not rect:continue
        width=item.get('width');height=item.get('height')
        if not width or not height:continue
        sx=(rect[2]-rect[0])*image.width/width;sy=(rect[3]-rect[1])*image.height/height
        for w in item.get('words',[]):
            if not isinstance(w.get('text'),str) or any(type(w.get(k)) not in (int,float) for k in ('x','y','w','h')):continue
            if w['w']<=0 or w['h']<=0:continue
            result.append(dict(text=clean(w['text']),x=rect[0]*image.width+w['x']*sx,
                y=rect[1]*image.height+w['y']*sy,w=w['w']*sx,h=w['h']*sy))
    return result


def lines(words):
    groups=[]
    for w in sorted(words,key=lambda w:(w['y'],w['x'])):
        row=next((g for g in groups if abs(g[0]['y']-w['y'])<=max(g[0]['h'],w['h'])*.55),None)
        if row is None:groups.append([w])
        else:row.append(w)
    return [dict(words=sorted(g,key=lambda w:w['x']),text=''.join(w['text'] for w in sorted(g,key=lambda w:w['x']))) for g in groups]


def stat_rows(words,n=6):
    expanded=expanded_words([w for row in lines(words) for w in row['words']])
    hits=[[w for w in expanded if re.fullmatch(label,w['text'],re.I)] for label in LABELS[:n]]
    found=[]
    for first in hits[0]:
        row=[first]
        for options in hits[1:]:
            possible=[w for w in options if abs(w['y']-first['y'])<=max(w['h'],first['h'])*.85 and w['x']>row[-1]['x']+row[-1]['w']*.5]
            if not possible:break
            row.append(min(possible,key=lambda w:abs(w['y']-first['y'])+w['x']*.001))
        if len(row)==n:
            centers=[w['x']+w['w']/2 for w in row];gaps=[b-a for a,b in zip(centers,centers[1:])]
            if max(gaps)<=min(gaps)*2.3 and not any(abs(first['y']-r[0]['y'])<first['h']*.5 for r in found):found.append(row)
    return found


def highlighted_tab(image,row):
    """Accept only a unique yellow active tab; otherwise leave it unknown."""
    centers=[w['x']+w['w']/2 for w in row];spacing=min(b-a for a,b in zip(centers,centers[1:]));scores=[]
    for center,w in zip(centers,row):
        rect=[max(0,(center-spacing*.45)/image.width),max(0,(w['y']-w['h']*2)/image.height),
              min(1,(center+spacing*.45)/image.width),min(1,(w['y']+w['h']*2)/image.height)]
        pic=crop(image,rect).resize((60,40));pixels=list(pic.get_flattened_data())
        scores.append(sum(r>150 and g>120 and b<min(r,g)*.75 and abs(r-g)<110 for r,g,b in pixels)/len(pixels))
    order=sorted(range(5),key=lambda i:scores[i],reverse=True);a,b=order[:2]
    return TYPES[a] if scores[a]>.035 and scores[a]>scores[b]+.025 and scores[a]>scores[b]*1.7 else None


def gain_row(image,words,anchors):
    centers=[w['x']+w['w']/2 for w in anchors];spacing=min(b-a for a,b in zip(centers,centers[1:]));h=max(w['h'] for w in anchors)
    expanded=expanded_words([w for row in lines(words) for w in row['words']])
    plus=[];seen=set()
    for w in expanded:
        key=(round(w['x']),round(w['y']),w['text'])
        if key in seen or not re.fullmatch(r'\+\d{1,3}',w['text']):continue
        seen.add(key)
        if abs(w['y']-anchors[0]['y'])<=h*7 and centers[0]-spacing*.5<w['x']+w['w']/2<centers[-1]+spacing*.5:plus.append(w)
    candidates=[]
    for seed in plus:
        cells={};ambiguous=False
        for w in plus:
            if abs(w['y']-seed['y'])>max(w['h'],seed['h'])*.65:continue
            index=min(range(6),key=lambda i:abs(centers[i]-(w['x']+w['w']/2)))
            if index in cells:ambiguous=True;break
            cells[index]=w
        if ambiguous or len(cells)<2:continue
        y1=min(w['y'] for w in cells.values());y2=max(w['y']+w['h'] for w in cells.values())
        values=[];safe=True
        for i,center in enumerate(centers):
            if i in cells:
                n=int(cells[i]['text'][1:]);values.append(n if n<=300 else None);continue
            # Blank means zero only inside a geometrically confirmed gain row
            # and only when no text-like foreground is present in that cell.
            rect=[max(0,(center-spacing*.44)/image.width),max(0,y1/image.height),min(1,(center+spacing*.44)/image.width),min(1,y2/image.height)]
            blank=not glyphs(crop(image,rect))
            values.append(0 if blank else None)
            if not blank:safe=False
        score=len(cells)*100-abs(seed['y']-anchors[0]['y'])/h
        rect=bounds(anchors+list(cells.values()),image,pad=max(3,h*.2))
        candidates.append((score,values,rect,safe))
    if not candidates:return None,None
    best=max(candidates,key=lambda c:c[0])
    return best[1],best[2]


def parse_screen(image,raw,regions):
    words=mapped_words(image,raw,regions);rows=lines(words);text=' '.join(r['text'] for r in rows)
    top=[r for r in rows if min(w['y'] for w in r['words'])<image.height*.4]
    date=parse_date(' '.join(r['text'] for r in top));date_words=[]
    for r in top:
        if re.search(r'初[級级]|古典|經典|经典|資深|资深|ジュニア|クラシック|シニア|[123]年|\d月|前半|[後后]半|回合|ターン|出道',r['text']):date_words+=r['words']
    selected=[]
    for r in rows:
        kind=selected_type(r['text'])
        if kind:selected.append((kind,r))
    names={x[0] for x in selected};kind=next(iter(names)) if len(names)==1 else None
    selected_rect=bounds(selected[0][1]['words'],image,4) if kind else None
    gains=None;gain_rect=None;anchors=None
    candidates=[]
    for row in stat_rows(words):
        values,rect=gain_row(image,words,row)
        if values is not None:candidates.append((row,values,rect))
    if len(candidates)==1:anchors,gains,gain_rect=candidates[0]
    if not kind and anchors:
        tabs=[r for r in stat_rows(words,5) if r[0]['y']>anchors[0]['y']+anchors[0]['h']*1.2]
        if len(tabs)==1:
            kind=highlighted_tab(image,tabs[0]);selected_rect=bounds(tabs[0],image,max(w['h'] for w in tabs[0])*2)
    failure=None;failure_rect=None
    for r in rows:
        value=parse_number(r['text'],r'(?:失[敗败]率|失[敗败])[:：]?(\d{1,3})%',100)
        if value is not None:
            if failure is not None and failure!=value:failure=None;failure_rect=None;break
            failure=value;failure_rect=bounds(r['words'],image,5)
    mood_match=re.search(r'(?:幹勁|干劲|やる気)[:：]?(超絕好調|絕好調|好調|普通|不調|絕不調)',text)
    fans=None;required=None;goaltext='';fan_rect=None;goal_rect=None
    for r in rows:
        if not re.search(r'粉[絲丝]|ファン',r['text']):continue
        is_goal=bool(re.search(r'目[標标]|至少|達到|达到|需要|不足|條件|条件',r['text']))
        value=parse_number(r['text'],r'(?:粉[絲丝](?:數|数)?|ファン[数數]?)(?:達到|达到|至少|需|需要|[:：])?(\d[\d,]*(?:\.\d+)?[萬万]?)',99_999_999)
        if is_goal:required=value;goaltext=r['text'][:120];goal_rect=bounds(r['words'],image,8)
        elif fans is None:fans=value;fan_rect=bounds(r['words'],image,8)
    # Keep the existing conservative event/boundary parser for background AI.
    irregular=parse_result(dict(items=[dict(key='header',text=' '.join(r['text'] for r in top)),dict(key='preview',text=text)],elapsed_ms=raw.get('elapsed_ms')))
    scene=irregular['scene'] if irregular['scene'] not in ('training','unknown') else 'training' if kind and failure is not None and gains is not None else 'unknown'
    p=dict(date=date,scene=scene,kind=kind,gains=gains,failure=failure,fans=fans,energy=None,
        mood=mood_match[1] if mood_match else None,attributes=None,goal_required=required,goal_text=goaltext,
        event_text=text[:1600],elapsed_ms=raw.get('elapsed_ms'),source='automatic_ocr',character=character_from_header(rows,image))
    layout={}
    if scene=='training' and (date.get('calendar_index') is not None or date.get('token')) and all((gain_rect,selected_rect,failure_rect,date_words)):
        layout=dict(gains=gain_rect,selected=selected_rect,failure=failure_rect,header=bounds(date_words,image,5))
        # Stat labels are a scene guard against menus/dialogs covering training.
        layout['guard']=bounds(anchors,image,2)
        if fan_rect:layout['fans']=fan_rect
        if goal_rect:layout['goal']=goal_rect
    return p,layout


class AutomaticChannel:
    def __init__(self,coach):
        self.coach=coach;self.path=coach.session.data_dir/'auto-layout.json';self.layout={}
        self.busy=False;self.pending=None;self.epoch=None;self.last_at=0;self.error_until=0
        self.cache={};self.previous=None;self.last_key=None;self.cached_observation=None;self.repeats=0
        self.live={};self.allow_attach=False;self.guard=None;self.ocr_calls=0;self.cache_hits=0;self.audit=0;self.previous_character=None
        try:
            stored=json.loads(self.path.read_text('utf-8'))
            keys=set(stored.get('regions',{}))
            if stored.get('version')==1 and {'gains','selected','failure','header','guard'}<=keys and not keys-{'gains','selected','failure','header','guard','fans','goal'} and all(rect_valid(r) for r in stored['regions'].values()):self.layout=stored['regions']
        except (OSError,ValueError,TypeError):pass

    def snapshot(self):
        return deepcopy(dict(busy=self.busy,located=bool(self.layout),ocr_calls=self.ocr_calls,cache_hits=self.cache_hits,live=self.live,
            message='自动定位与变化区域读取；无需采样、无需登录。图形未确认时不猜人数或指南条。'))

    def _key(self,image):
        regions=self.layout or dict(screen=[0,0,1,1])
        return tuple((k,fingerprint(image,r)) for k,r in regions.items())

    def _apply(self,p,image):
        s=self.coach.session
        if self.allow_attach and not s.tracker.active and p['scene']=='training' and (p['date'].get('calendar_index') is not None or p['date'].get('token')):
            s.tracker.new();self.allow_attach=False
        name=p.get('character');run=s.tracker.active
        if name and name==self.previous_character and run and not s.tracker.pending and not run.character_locked and not run.character:
            run.character=name
        self.previous_character=name
        self.coach._apply(p)
        if not s.tracker.pending:
            if p['scene']=='training' and (p['gains'] is None or any(v is None for v in p['gains'])):
                self.coach.message='部分收益尚未读清，正在自动重试；保持该训练预览片刻即可'
                self.audit=min(self.audit,time.monotonic()-19.5)
            elif self.coach.live_date is None:
                self.coach.message='正在自动确认日期与画面归属，保持训练预览片刻即可'
        self.coach.route_irregular(p,image)

    def submit(self,image,force=False):
        s=self.coach.session;epoch=s.epoch
        if self.epoch!=epoch:
            if self.busy:self.pending=(image,force,epoch);return
            self.epoch=epoch;self.cache={};self.last_key=None;self.cached_observation=None;self.repeats=0;self.coach.stable.previous={};self.guard=None;self.previous_character=None
        now=time.monotonic()
        if not force and now<self.error_until:return
        key=self._key(image)
        if not self.busy and key==self.last_key and self.cached_observation and now-self.audit<20:
            self.cache_hits+=1
            if self.repeats<2:self._apply(deepcopy(self.cached_observation),image);self.repeats+=1
            elif (run:=s.tracker.active) and self.coach.live_date is not None:
                item=run.local.get('training',{}).get(self.cached_observation['kind'])
                if item:item['at']=time.time()
            return
        # A changed date/scene cannot leave yesterday's advice visible while OCR
        # is in flight. Unknown selection is automatically retried, not calibrated.
        if self.last_key and (dict(key).get('header')!=dict(self.last_key).get('header') or dict(key).get('guard')!=dict(self.last_key).get('guard')):
            self.coach.live_date=None;self.coach.live_scene='unknown'
        if self.busy:self.pending=(image,force,epoch);return
        if not force and now-self.last_at<.15:return
        self.busy=True;self.last_at=now
        regions={k:r for k,r in (self.layout or dict(screen=[0,0,1,1])).items() if k!='guard'}
        old_cache=deepcopy(self.cache)
        def work():
            started=time.perf_counter()
            try:
                changed={k:r for k,r in regions.items() if force or now-self.audit>=20 or old_cache.get(k,{}).get('hash')!=fingerprint(image,r)}
                self.coach.message='正在自动定位训练界面…' if not self.layout else '正在本地读取变化的训练区域…'
                raw=self.coach.ocr.recognize(image,changed);items={item['key']:item for item in raw.get('items',[])}
                for k,r in regions.items():
                    if k not in items and k in old_cache:items[k]=old_cache[k]['item']
                combined=dict(items=list(items.values()),elapsed_ms=raw.get('elapsed_ms'))
                parsed,layout=parse_screen(image,combined,regions)
                elapsed=round((time.perf_counter()-started)*1000,1)
                with s.lock:
                    if epoch!=s.epoch:return
                    self.ocr_calls+=1;self.coach.elapsed_ms=elapsed;self.error_until=0
                    self.cache={k:dict(hash=fingerprint(image,r),item=items[k]) for k,r in regions.items() if k in items}
                    if layout and layout!=self.layout:
                        self.layout=layout;self.cache={};self.path.parent.mkdir(parents=True,exist_ok=True)
                        temp=self.path.with_suffix('.tmp');temp.write_text(json.dumps(dict(version=1,regions=layout),ensure_ascii=False),'utf-8');temp.replace(self.path)
                    elif not layout and parsed['scene']!='training':
                        self.layout={};self.cache={}
                    self.live=dict(scene=parsed['scene'],date=parsed['date'],kind=parsed['kind'],gains=parsed['gains'],failure=parsed['failure'],elapsed_ms=elapsed)
                    self.last_key=self._key(image);self.cached_observation=deepcopy(parsed);self.repeats=1;self.audit=now
                    self._apply(parsed,image)
                    if parsed['scene']=='unknown':
                        self.coach.message='正在自动寻找训练界面；请在游戏中打开任一训练预览，无需采样或框选'
                        # An unrecognised screen is retried, never cached forever.
                        self.audit=now-19
            except OcrError as error:
                with s.lock:
                    if epoch==s.epoch:
                        self.error_until=time.monotonic()+30;self.coach.live_date=None;self.coach.live_scene='unknown'
                        self.coach.message=str(error)+'；自动模式无需采样，但需Windows本机OCR可用'
            except Exception:
                with s.lock:
                    if epoch==s.epoch:
                        self.layout={};self.cache={};self.last_key=None;self.coach.live_date=None;self.coach.live_scene='unknown'
                        self.error_until=time.monotonic()+1;self.coach.message='正在重新定位画面；保持任一训练预览即可，无需配置模板'
            finally:
                with s.lock:
                    self.busy=False;pending=self.pending;self.pending=None
                    if pending and pending[2]==s.epoch:self.submit(pending[0],pending[1])
        threading.Thread(target=work,daemon=True).start()
