"""Persistent Windows OCR process and conservative, layout-aware text parsing."""
from copy import deepcopy
import json
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata

DEFAULT_REGIONS = dict(header=[0, 0, 1, .38], preview=[0, .38, 1, 1],
                       selected=None, fans=None, goal=None, stats=None)
LABELS = [r'速度|スピード', r'持久力|耐力|スタミナ', r'力量|パワー',
          r'意志力|毅力|根性|ガッツ', r'智力|賢さ', r'技能(?:Pt|点|點)|スキルPt|SkillPt']
TYPES = ['速度','耐力','力量','毅力','智力']


class OcrError(RuntimeError):
    pass


class WindowsOcr:
    def __init__(self, data_dir):
        self.data_dir=Path(data_dir)
        self.process=None
        self.lines=queue.Queue()
        self.lock=threading.Lock()
        self.language=''
        self.message='尚未启动本地OCR'

    def close(self):
        if self.process:
            self.process.terminate()
            try:self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=2)
            self.process=None

    def _line(self, timeout=15):
        try:
            line=self.lines.get(timeout=timeout)
            if line is None:raise OcrError('Windows OCR进程已退出，请重启助手')
            return json.loads(line)
        except (queue.Empty,ValueError):
            self.close()
            raise OcrError('本地OCR未响应；请检查Windows文字识别组件，或使用模型分析') from None

    def _start(self):
        if sys.platform!='win32':
            raise OcrError('Windows本地OCR仅支持Windows；当前可用手动快速录入或模型分析')
        if self.process and self.process.poll() is None:return
        self.lines=queue.Queue()
        # Launch the inbox Windows PowerShell, never PATH-resolved third-party code.
        import os
        executable=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        # Fixed bundled command; stdin is only JSON image jobs, never commands.
        # No execution-policy setting is changed and no external script is fetched.
        command=Path(__file__).with_name('ocr_windows.ps1').read_text('utf-8')
        self.process=subprocess.Popen([str(executable),'-NoLogo','-NoProfile','-NonInteractive','-Command',
            command],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,text=True,encoding='utf-8',bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW)
        stream, messages=self.process.stdout,self.lines
        def consume():
            try:
                for line in stream:messages.put(line)
            finally:messages.put(None)
        threading.Thread(target=consume,daemon=True).start()
        status=self._line()
        if not status.get('ready'):
            self.close()
            if status.get('error')=='language_missing':
                raise OcrError('Windows未安装中文/日文OCR语言组件；请在系统语言设置添加文字识别功能，或使用快速录入/模型分析')
            raise OcrError('Windows OCR不可用或PowerShell脚本受系统策略限制；请使用快速录入或模型分析')
        self.language=status.get('language','')

    def recognize(self,image,regions):
        with self.lock:
            self._start()
            self.data_dir.mkdir(parents=True,exist_ok=True)
            started=time.monotonic()
            with tempfile.TemporaryDirectory(dir=self.data_dir) as tmp:
                items=[]
                for key,rect in regions.items():
                    if rect is None:continue
                    x1,y1,x2,y2=rect
                    pic=image.crop((round(x1*image.width),round(y1*image.height),round(x2*image.width),round(y2*image.height))).convert('RGB')
                    # Enlarge small UI text, bounded by Windows OCR image limits.
                    factor=min(2,1800/max(pic.size))
                    pic=pic.resize((max(1,round(pic.width*factor)),max(1,round(pic.height*factor))))
                    path=Path(tmp)/(key+'.png');pic.save(path)
                    items.append(dict(key=key,path=str(path.resolve())))
                if not items:raise OcrError('请先配置OCR选区')
                try:
                    self.process.stdin.write(json.dumps(dict(items=items))+'\n');self.process.stdin.flush()
                except (OSError,BrokenPipeError):
                    self.close();raise OcrError('本地OCR进程中断，请重试') from None
                result=self._line()
            if not result.get('ok'):raise OcrError('本地文字识别失败；请让游戏可见并校准OCR选区')
            return dict(items=result['items'],language=self.language,elapsed_ms=round((time.monotonic()-started)*1000))


def clean(text):
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',text or ''))


def parse_date(text):
    value=clean(text)
    year=None
    for pattern,number in [(r'初[級级]|新[馬马]|ジュニア|Junior',1),(r'古典|經典|经典|クラシック|Classic',2),
                            (r'資深|资深|高[級级]|シニア|Senior',3)]:
        if re.search(pattern,value,re.I):year=number;break
    if year is None:
        match=re.search(r'([123])年(?:目)?',value)
        if match:year=int(match[1])
    month=re.search(r'(?<!\d)(1[012]|[1-9])月(前半|後半|后半)',value)
    remaining=re.search(r'(?:距.{0,10}?|剩[餘余]?|あと|還有|还有)(\d{1,2})(?:回合|ターン)',value)
    result=dict(date_text=None,calendar_index=None,remaining=int(remaining[1]) if remaining else None)
    if year is not None and month:
        m=int(month[1]);half=1 if month[2] in ('後半','后半') else 0
        result.update(date_text=f'{["初級","古典","資深"][year-1]}年{m}月{"後半" if half else "前半"}',
                      calendar_index=(year-1)*24+(m-1)*2+half)
    elif remaining and re.search(r'出道|デビュー',value):
        result.update(date_text='出道前',token='debut:'+remaining[1])
    return result


def parse_number(text,pattern,maximum):
    m=re.search(pattern,clean(text),re.I)
    if not m:return None
    if re.match(r'[A-Za-z0-9,萬万]',clean(text)[m.end():]):return None
    raw=m[1].replace(',','')
    value=float(raw[:-1])*10000 if raw.endswith(('萬','万')) else float(raw)
    return int(value) if value.is_integer() and 0<=value<=maximum else None


def selected_type(text):
    value=clean(text)
    matches=[]
    for i,label in enumerate(LABELS[:5]):
        if re.search('(?:'+label+')(?:訓練|训练|トレーニング)',value):matches.append(TYPES[i])
    return matches[0] if len(matches)==1 else None


def parse_gains(region, complete=False):
    words=expanded_words(region.get('words',[]))
    anchors=[]
    for index,label in enumerate(LABELS):
        hits=[w for w in words if re.fullmatch(label,clean(w['text']),re.I)]
        if len(hits)!=1:return None
        anchors.append(hits[0])
    # Ambiguous duplicate stat rows are rejected rather than guessed.
    if max(a['y'] for a in anchors)-min(a['y'] for a in anchors)>max(a['h'] for a in anchors)*2:return None
    centers=[a['x']+a['w']/2 for a in anchors]
    if centers!=sorted(centers):return None
    plus=[w for w in words if re.fullmatch(r'\+\d{1,3}',clean(w['text']))]
    gains=[0 if complete else None for _ in range(6)]
    seen=set()
    for w in plus:
        index=min(range(6),key=lambda i:abs(centers[i]-(w['x']+w['w']/2)))
        if index in seen:return None
        seen.add(index)
        gains[index]=int(clean(w['text'])[1:])
    return gains if plus else None


def parse_result(raw,complete=False):
    regions={item['key']:item for item in raw.get('items',[])}
    header=regions.get('header',{}).get('text','');preview=regions.get('preview',{})
    text=clean(header+' '+preview.get('text',''))
    date=parse_date(header)
    selected=regions.get('selected',{}).get('text','')
    kind=selected_type(selected or preview.get('text',''))
    if selected and not kind:
        hits=[TYPES[i] for i,label in enumerate(LABELS[:5]) if re.fullmatch('(?:'+label+')(?:Lv\\.?[1-5])?',clean(selected),re.I)]
        kind=hits[0] if len(hits)==1 else None
    scene='training' if kind and ('失敗' in text or '失败' in text or '訓練' in text or '训练' in text or 'トレーニング' in text) else 'unknown'
    if re.search(r'心得.{0,5}(選擇|选择)|ヒント.{0,3}獲得|選擇心得',text):scene='event'
    if re.search(r'粉[絲丝](?:數|数)?不足|ファン数不足|出賽條件|目標レース',text):scene='race'
    if re.search(r'確定要放棄|是否放弃|あきらめますか',text):scene='abandon_dialog'
    if re.search(r'已放棄養成|育成をあきらめました',text):scene='abandon_confirmed'
    if re.search(r'養成結束|育成完了|養成結算',text) and re.search(r'評[價价]|評価|獲得|能力',text):scene='result'
    if scene=='unknown' and re.search(r'養成開始|育成開始',text):scene='run_start'
    goaltext=regions.get('goal',{}).get('text','')
    fans=parse_number(regions.get('fans',{}).get('text',''),r'(?:粉[絲丝](?:數|数)?|ファン[数數]?)[:：]?(\d[\d,]*(?:\.\d+)?[萬万]?)',99_999_999)
    required=parse_number(goaltext,r'(?:粉[絲丝](?:數|数)?|ファン[数數]?)(?:達到|达到|至少|需|需要|[:：])?(\d[\d,]*(?:\.\d+)?[萬万]?)',99_999_999)
    failure=parse_number(preview.get('text',''),r'(?:失[敗败]率|失[敗败])[:：]?(\d{1,3})%',100)
    energy=re.search(r'(?:體力|体力)[:：]?(\d{1,3})/(\d{1,3})',text)
    mood=re.search(r'(?:幹勁|干劲|やる気)[:：]?(超絕好調|絕好調|好調|普通|不調|絕不調)',text)
    return dict(date=date,scene=scene,kind=kind,gains=parse_gains(preview,complete),failure=failure,
        fans=fans,goal_required=required,goal_text=clean(goaltext)[:120],
        energy=dict(current=int(energy[1]),maximum=int(energy[2])) if energy and int(energy[1])<=int(energy[2])<=200 else None,
        mood=mood[1] if mood else None,attributes=parse_attributes(regions.get('stats',{})),event_text=text[:1600],
        elapsed_ms=raw.get('elapsed_ms'),language=raw.get('language',''))


def parse_attributes(region):
    words=expanded_words(region.get('words',[]));anchors=[]
    for label in LABELS[:5]:
        hits=[w for w in words if re.fullmatch(label,clean(w['text']),re.I)]
        if len(hits)!=1:return None
        anchors.append(hits[0])
    centers=[a['x']+a['w']/2 for a in anchors]
    if centers!=sorted(centers):return None
    values=[[] for _ in range(5)]
    for word in words:
        if not re.fullmatch(r'\d{1,4}',clean(word['text'])):continue
        index=min(range(5),key=lambda i:abs(centers[i]-(word['x']+word['w']/2)))
        if int(clean(word['text']))<=2500:values[index].append(int(clean(word['text'])))
    if any(len(v)!=1 for v in values):return None
    return dict(zip(('speed','stamina','power','guts','wisdom'),[v[0] for v in values]))


def expanded_words(words):
    """Windows Chinese OCR may split a label into individual characters."""
    result=list(words)
    for start,first in enumerate(words):
        text=clean(first['text']);last=first
        for word in words[start+1:start+6]:
            if abs(word['y']-first['y'])>max(first['h'],word['h'])*.6 or word['x']-(last['x']+last['w'])>max(first['h'],word['h'])*1.6:
                break
            text+=clean(word['text']);last=word
            if any(re.fullmatch(label,text,re.I) for label in LABELS) or re.fullmatch(r'\+\d{1,3}',text):
                result.append(dict(text=text,x=first['x'],y=first['y'],w=last['x']+last['w']-first['x'],h=max(first['h'],last['h'])))
    return result


class StableReadings:
    """Two matching readings, not OCR's nonexistent confidence score."""
    def __init__(self):self.previous={}

    def confirm(self,values):
        confirmed={}
        for key,value in values.items():
            if value is not None and self.previous.get(key)==value:confirmed[key]=deepcopy(value)
        self.previous=deepcopy(values)
        return confirmed
