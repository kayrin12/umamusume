"""Changed-region template inference; date OCR runs on its own worker."""
from copy import deepcopy
import hashlib
import threading
import time
from .templates import ATTRS,TemplateBank,crop
from .local_ocr import parse_date,parse_result


def digest(pic):return hashlib.blake2b(pic.tobytes(),digest_size=12).hexdigest()


class FastChannel:
    def __init__(self,coach):
        self.coach=coach;self.bank=TemplateBank(coach.session.data_dir/'templates')
        self.lock=threading.RLock();self.busy=False;self.date_busy=False
        self.last_date_hash=None;self.date_result=None;self.date_error=None;self.date_requested=0
        self.live={};self.epoch=None;self.manual_date=None
        self.previous_signature=None;self.last_apply=0
        self.repeat_count=0
        self.pending=None

    def snapshot(self):
        with self.lock:return dict(bank=self.bank.status(),live=deepcopy(self.live),busy=self.busy,date_busy=self.date_busy,date_error=self.date_error)

    def set_manual_date(self,image,date):
        rect=self.bank.data['regions'].get('date',{}).get('rect') or self.coach.settings['regions'].get('header')
        self.manual_date=(digest(crop(image,rect)),deepcopy(date)) if image is not None and rect else None

    def _date(self,image,values,header_hash,rect,epoch):
        if all(values.get(k) is not None for k in ('year','month','half')):
            date=parse_date(f'{values["year"]}年{values["month"]}月{values["half"]}')
            date['remaining']=values.get('remaining');return date
        if self.manual_date and self.manual_date[0]==header_hash:return deepcopy(self.manual_date[1])
        if self.last_date_hash==header_hash and self.date_result:return deepcopy(self.date_result)
        now=time.monotonic()
        if not self.date_busy and now-self.date_requested>=1 and rect:
            self.date_busy=True;self.date_requested=now
            regions={'header':rect}
            # Irregular scenes may use background text OCR; this never gates digits.
            if values.get('selected') is None and self.coach.settings['regions'].get('preview'):
                regions['preview']=self.coach.settings['regions']['preview']
            def work():
                try:
                    raw=self.coach.ocr.recognize(image,regions);parsed=parse_result(raw)
                    with self.coach.session.lock:
                        if epoch!=self.coach.session.epoch:return
                        self.last_date_hash=header_hash;self.date_result=parsed['date'];self.date_error=None
                        self.coach.route_irregular(parsed,image)
                except Exception:
                    with self.coach.session.lock:
                        if epoch==self.coach.session.epoch:self.date_error='日期OCR未可用；可采集学年/月份/前后半模板，或手动确认日期'
                finally:self.date_busy=False
            threading.Thread(target=work,daemon=True).start()
        return dict(date_text=None,calendar_index=None,remaining=values.get('remaining'))

    def submit(self,image,force=False):
        if self.busy:
            self.pending=(image,force,self.coach.session.epoch)
            return
        self.busy=True;epoch=self.coach.session.epoch
        def work():
            started=time.perf_counter()
            try:
                if self.epoch!=epoch:
                    self.epoch=epoch;self.previous_signature=None;self.coach.stable.previous={}
                with self.lock:
                    result=self.bank.recognize(image)
                    rect=self.bank.data['regions'].get('date',{}).get('rect') or self.coach.settings['regions'].get('header')
                header_hash=digest(crop(image,rect)) if rect else None
                values=result['values'];date=self._date(image,values,header_hash,rect,epoch)
                cards=[]
                for slot in range(6):
                    avatar=values.get(f'card{slot}_avatar')
                    def flag(name):
                        v=values.get(f'card{slot}_{name}');return True if v=='yes' else False if v=='no' else None
                    cards.append(dict(slot=slot+1,name=avatar,friendship=flag('friendship'),bond_full=flag('bond'),hint=flag('hint')))
                present=[card for card in cards if card['name'] not in (None,'empty')]
                complete=all(card['name'] is not None for card in cards)
                def count(field):return sum(card[field] for card in present) if complete and all(card[field] is not None for card in present) else None
                counts=dict(participants=len(present) if complete else None,friends=count('friendship'),hints=count('hint'),
                    unbonded=sum(card['bond_full'] is False for card in present) if complete and all(card['bond_full'] is not None for card in present) else None)
                gains=[values.get('gain_'+key) for key in ATTRS+['skill']]
                gauges={key:values.get('guide_'+key) for key in ('blue','green','pink')}
                p=dict(date=date,scene='training' if values.get('selected') else 'unknown',kind=values.get('selected'),gains=gains,
                    failure=values.get('failure'),fans=values.get('fans'),energy=None,mood=values.get('mood'),
                    attributes={key:values.get('stat_'+key) for key in ATTRS} if all(values.get('stat_'+key) is not None for key in ATTRS) else None,
                    goal_required=None,goal_text='',event_text='',elapsed_ms=result['metrics']['elapsed_ms'],
                    source='self_templates',extras=counts|dict(color=values.get('fragment'),guide_gauges=gauges))
                p['guide_gauges']=gauges if any(n is not None for n in gauges.values()) else None
                live=dict(values=values,quality=result['quality'],field_hashes=result['field_hashes'],cards=cards,counts=counts,energy_percent=values.get('energy'),
                    mood=values.get('mood'),ailment=True if values.get('ailment')=='yes' else False if values.get('ailment')=='no' else None,
                    date=date,metrics=result['metrics'],notice='体力是像素比例估计；相似度不是概率。未知项不按0计算。')
                live['metrics']['pipeline_ms']=round((time.perf_counter()-started)*1000,2)
                live['metrics']['over_budget']=live['metrics']['pipeline_ms']>300
                with self.coach.session.lock:
                    if epoch!=self.coach.session.epoch:return
                    self.live=live;self.coach.elapsed_ms=live['metrics']['pipeline_ms']
                    run=self.coach.session.tracker.active
                    context=(round(values['energy']/5) if values.get('energy') is not None else None,values.get('mood'),values.get('ailment'))
                    if run and run.local.get('pixel_context')!=list(context):run.local['training']={};run.local['pixel_context']=list(context)
                    stamp=repr(({k:v for k,v in p.items() if k!='elapsed_ms'},header_hash))
                    # Reuse exact field results for stability; do not repeatedly save
                    # the same unchanged frame or re-run expensive classifications.
                    now=time.monotonic()
                    if stamp!=self.previous_signature:self.repeat_count=0
                    if self.repeat_count<2:
                        self.coach._apply(p);self.previous_signature=stamp;self.last_apply=now;self.repeat_count+=1
                        if p['kind'] and self.bank.status()['missing_digits']:
                            self.coach.message='请先采集完整0–9及+数字模板；缺少字形时不猜收益'
                    else:
                        run=self.coach.session.tracker.active
                        if run and p['kind'] in run.local.get('training',{}) and self.coach.live_date is not None:
                            run.local['training'][p['kind']]['at']=time.time()
            except Exception:
                with self.coach.session.lock:
                    if epoch==self.coach.session.epoch:
                        self.coach.live_scene='unknown';self.coach.live_date=None
                        self.coach.message='模板识别未能校验；请检查采样与选区，或快速修正'
            finally:
                self.busy=False
                pending=self.pending;self.pending=None
                if pending and pending[2]==self.coach.session.epoch:self.submit(pending[0],pending[1])
        threading.Thread(target=work,daemon=True).start()
