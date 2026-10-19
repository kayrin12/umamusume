"""Fast local channel; independent of paid model request rate and latency."""
from copy import deepcopy
import json
import sys
import threading
import time
from .local_ocr import DEFAULT_REGIONS, OcrError, StableReadings, WindowsOcr, parse_result
from .training import ranking, validate_preview
from .schedule import alerts, find_profile


class LocalError(ValueError):
    pass


def signature(state):
    return json.dumps({k:state.get(k) for k in ('mentor','energy','mood','insights','guide_gauges','attributes')},sort_keys=True,ensure_ascii=False)


def date_key(date):
    return date.get('calendar_index') if date.get('calendar_index') is not None else date.get('token')


class LocalCoach:
    def __init__(self,session):
        self.session=session
        self.path=session.data_dir/'local_settings.json'
        self.settings=dict(enabled=sys.platform=='win32',regions=deepcopy(DEFAULT_REGIONS),preview_complete=False,risk_limit=15)
        if self.path.exists():
            try:self.configure_values(json.loads(self.path.read_text('utf-8')))
            except (ValueError,TypeError,KeyError):pass
        self.ocr=WindowsOcr(session.data_dir/'ocr-tmp')
        self.stable=StableReadings()
        self.busy=False
        self.message='本地OCR等待读取；训练无需等待模型' if self.settings['enabled'] else '本地OCR未启用；可用快速录入'
        self.last_at=float('-inf')
        self.last_epoch=None
        self.error_until=0
        self.live_scene='unknown'
        self.live_date=None
        self.elapsed_ms=None
        self.event_key=None
        self.generation=0

    def configure_values(self,values):
        if not isinstance(values,dict) or set(values)-set(self.settings):raise LocalError('本地设置字段无效')
        if 'enabled' in values and type(values['enabled']) is not bool:raise LocalError('启用状态无效')
        if 'preview_complete' in values and type(values['preview_complete']) is not bool:raise LocalError('完整选区状态无效')
        if 'risk_limit' in values and (type(values['risk_limit']) is not int or not 0<=values['risk_limit']<=50):raise LocalError('失败率上限应为0到50')
        if 'regions' in values:
            if not isinstance(values['regions'],dict) or set(values['regions'])-set(DEFAULT_REGIONS):raise LocalError('OCR选区无效')
            for key,rect in values['regions'].items():
                if rect is not None and (not isinstance(rect,list) or len(rect)!=4 or any(type(n) not in (int,float) or not 0<=n<=1 for n in rect) or rect[2]-rect[0]<.01 or rect[3]-rect[1]<.01):
                    raise LocalError('选区必须在画面内且至少占宽高的1%')
            values=deepcopy(values);values['regions']=self.settings['regions']|values['regions']
        self.settings.update(deepcopy(values))

    def configure(self,values):
        self.configure_values(values)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.path.write_text(json.dumps(self.settings,ensure_ascii=False,indent=2),'utf-8')
        self.generation+=1;self.stable=StableReadings();self.live_date=None;self.live_scene='unknown';self.error_until=0
        self.session.auto=False;self.session.epoch+=1;self.session.result=None
        run=self.session.tracker.active
        if run:run.local['training']={};self.session.tracker.save()
        self.message='本地设置已保存；请开启持续读取，切换预览会自动记录'

    def _run(self,run_id):
        run=self.session.tracker.active
        if not run or run.id!=run_id or self.session.tracker.pending or self.session.sample:
            raise LocalError('请先确认当前局次；样本模式不能修改本局数据')
        return run

    def manual(self,run_id,values):
        run=self._run(run_id)
        allowed={'profile_id','character','calendar_index','remaining','turn_number','fans','preview','preview_date','goals'}
        if not isinstance(values,dict) or set(values)-allowed:raise LocalError('快速录入字段无效')
        local=deepcopy(run.local);character=run.character;state=deepcopy(run.state)
        if 'profile_id' in values:
            profile=find_profile(values['profile_id'])
            if not profile:raise LocalError('请选择有效角色版本')
            local['profile_id']=profile['id'];character=profile['name']
        if 'character' in values:
            name=values['character']
            if not isinstance(name,str) or not name.strip() or len(name)>80:raise LocalError('请输入本局角色名称')
            character=name.strip()
            profile=find_profile(local.get('profile_id'))
            if profile and character!=profile['name']:local.pop('profile_id',None)
        for key,low,high in [('calendar_index',0,71),('remaining',0,99),('turn_number',1,99),('fans',0,99_999_999)]:
            if key in values and values[key] is not None and (type(values[key]) is not int or not low<=values[key]<=high):
                raise LocalError(key+'数值无效')
        if 'calendar_index' in values:
            idx=values['calendar_index']
            if idx is None:raise LocalError('请填写完整学年、月份和前后半')
            if local.get('date',{}).get('calendar_index')!=idx:local['training']={}
            local['date']=dict(calendar_index=idx,date_text=date_label(idx),remaining=values.get('remaining'),source='manual')
        elif 'remaining' in values:local.setdefault('date',{})['remaining']=values['remaining']
        if 'turn_number' in values:
            idx=local.get('date',{}).get('calendar_index')
            if idx is None:raise LocalError('先填写日期，才能标定本局回合')
            local['anchor']=dict(calendar_index=idx,turn_number=values['turn_number']) if values['turn_number'] is not None else None
        if 'fans' in values:
            state['fans']=values['fans']
            state['evidence']['fans']=dict(source='manual',confidence=1,evidence='快速录入确认',turn_index=local.get('date',{}).get('calendar_index'),date_text='',updated_at=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
        if 'goals' in values:
            goals=values['goals']
            if not isinstance(goals,list) or len(goals)>30:raise LocalError('目标最多30项')
            for g in goals:
                if not isinstance(g,dict) or set(g)!={'name','deadline','required_fans','done'} or not isinstance(g['name'],str) or not 1<=len(g['name'])<=120 or type(g['deadline']) is not int or not 0<=g['deadline']<=71 or type(g['required_fans']) is not int or not 0<=g['required_fans']<=99_999_999 or type(g['done']) is not bool:
                    raise LocalError('请完整填写目标名称、期限、所需粉丝和完成状态')
                g['source']='user_confirmed'
            local['goals']=deepcopy(goals)
        if 'preview' in values:
            try:preview=validate_preview(values['preview'])
            except ValueError as e:raise LocalError(str(e)) from None
            idx=local.get('date',{}).get('calendar_index')
            if date_key(local.get('date',{})) is None:raise LocalError('先确认本回合日期，再录入训练预览')
            if values.get('preview_date')!=date_key(local.get('date',{})):raise LocalError('训练录入的回合已变化，请重新确认日期与收益')
            if local.get('cache_signature')!=signature(state):local['training']={}
            local['cache_signature']=signature(state)
            local.setdefault('training',{})[preview['kind']]=dict(preview=preview,at=time.time(),source='manual')
        run.local=local;run.state=state;run.character=character
        if 'profile_id' in values or 'character' in values:run.character_locked=True
        if 'calendar_index' in values:run.last_turn=values['calendar_index']
        run.state_revision+=1
        self.session.tracker.save();self.session.epoch+=1;self.session.result=None
        self.generation+=1;self.stable=StableReadings()
        if 'preview' in values or 'calendar_index' in values:
            self.live_scene='training';self.live_date=date_key(local.get('date',{}))
        self.message='本局快速数据已保存；切换预览可继续自动读取'

    def snapshot(self):
        run=self.session.tracker.active
        local=deepcopy(run.local) if run else {}
        date=local.get('date',{})
        idx=date.get('calendar_index')
        number=None
        if idx is not None and local.get('anchor'):
            number=local['anchor']['turn_number']+idx-local['anchor']['calendar_index']
        usable=bool(run and not self.session.tracker.pending and not self.session.sample and self.live_scene=='training' and self.live_date==date_key(date) and date_key(date) is not None)
        cache=local.get('training',{}) if run and local.get('cache_signature')==signature(run.state) else {}
        ranked=ranking(cache,run.state,idx,self.settings['risk_limit']) if usable else None
        schedule=alerts(run) if run else None
        if ranked and schedule['urgent']:ranked['recommendation']='先处理粉丝门槛，再决定训练。'+ranked['recommendation']
        return dict(settings=deepcopy(self.settings),busy=self.busy,message=self.message,elapsed_ms=self.elapsed_ms,
            scene=self.live_scene,date=date,turn_number=number,character_locked=run.character_locked if run else False,
            fans=run.state.get('fans') if run else None,profile_id=local.get('profile_id'),goals=local.get('goals',[]),
            training=ranked,schedule=schedule)

    def submit(self,frame,force=False):
        if self.busy:return
        now=time.monotonic()
        if not force and (now-self.last_at<.65 or now<self.error_until):return
        self.busy=True;self.last_at=now
        epoch=self.session.epoch;generation=self.generation
        if self.last_epoch!=epoch:self.stable=StableReadings();self.last_epoch=epoch
        settings=deepcopy(self.settings)
        def work():
            try:
                raw=self.ocr.recognize(frame,settings['regions'])
                parsed=parse_result(raw,settings['preview_complete'])
                with self.session.lock:
                    if epoch!=self.session.epoch or generation!=self.generation:return
                    self.elapsed_ms=parsed['elapsed_ms']
                    self._apply(parsed)
                    irregular=parsed['scene'] in ('event','race','result','abandon_dialog','abandon_confirmed','run_start')
                    if not irregular:self.event_key=None
                    if (irregular and parsed['event_text']!=self.event_key and self.session.auto and not self.session.busy and self.session.analysis_ready()
                            and (not self.session.tracker.pending or parsed['scene']=='run_start') and time.monotonic()-self.session.requested_at>=30):
                        self.event_key=parsed['event_text'];self.session.submit(frame,False)
            except OcrError as error:
                with self.session.lock:
                    if epoch==self.session.epoch:
                        self.message=str(error);self.error_until=time.monotonic()+60
                        self.live_scene='unknown';self.live_date=None
            except Exception:
                with self.session.lock:
                    if epoch==self.session.epoch:
                        self.message='本地OCR结果无法校验；请校准区域或使用快速录入/模型分析'
                        self.live_scene='unknown';self.live_date=None
            finally:
                with self.session.lock:self.busy=False
        threading.Thread(target=work,daemon=True).start()

    def _apply(self,p):
        self.live_scene=p['scene'];self.live_date=None
        run=self.session.tracker.active
        confirmed=self.stable.confirm(dict(date=p['date'] if date_key(p['date']) is not None else None,
            preview=dict(kind=p['kind'],gains=p['gains'],failure=p['failure']) if p['kind'] and p['gains'] is not None and p['failure'] is not None else None,
            fans=p['fans'],energy=p['energy'],mood=p['mood'],attributes=p['attributes'],goal=dict(text=p['goal_text'],required=p['goal_required']) if p['goal_required'] is not None else None))
        if not run or self.session.tracker.pending or self.session.sample:
            self.message='已读取本地文字；请先用“记录为新局”或“继续当前局”确认归属'
            return
        if 'date' not in confirmed:
            self.message='日期尚未稳定识别；请保持画面片刻或校准日期区域，不使用旧回合收益'
            return
        date=confirmed['date'];idx=date['calendar_index']
        previous_date=run.local.get('date',{})
        rollback=run.last_turn is not None and (idx is None or idx<run.last_turn)
        if (date.get('token') and previous_date.get('token') and date['remaining']>previous_date['remaining']):rollback=True
        if rollback:
            self.session.tracker.pending=True;run.local['training']={};self.session.result=None
            self.session.tracker.save();self.message='日期倒退，疑似重开；请确认局次，已停止训练建议';return
        local=run.local
        if 'fans' in confirmed and run.state.get('fans') is not None and confirmed['fans']<run.state['fans']:
            self.session.tracker.pending=True;local['training']={};self.session.result=None
            self.session.tracker.save();self.message='粉丝数倒退，可能重开或OCR异常；请确认局次及粉丝，暂停本地建议';return
        if date_key(local.get('date',{}))!=date_key(date):local['training']={}
        local['date']=date|dict(source='local_ocr_twice')
        if idx is not None:run.last_turn=idx
        self.live_date=date_key(date)
        if 'fans' in confirmed:run.state['fans']=confirmed['fans']
        for key in ('energy','mood','attributes'):
            if key in confirmed:run.state[key]=confirmed[key]
        for key in ('fans','energy','mood','attributes'):
            if key in confirmed:run.state['evidence'][key]=dict(source='local_ocr_twice',confidence=None,
                evidence='连续两次OCR一致（非OCR置信度）',turn_index=idx,date_text=date['date_text'],updated_at=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
        context=signature(run.state)
        if local.get('cache_signature')!=context:local['training']={};local['cache_signature']=context
        if p['scene'] not in ('training','unknown'):local['training']={}
        if p['scene']=='training' and 'preview' in confirmed:
            try:preview=validate_preview(confirmed['preview'])
            except ValueError:
                self.message='日期已识别；训练收益不完整，请校准含六列标题的完整收益区域，或快速录入'
            else:
                # Do not attach previous-screen confirmation to a newly changed date.
                local.setdefault('training',{})[preview['kind']]=dict(preview=preview,at=time.time(),source='local_ocr_twice')
                self.message='本回合训练已自动缓存；继续切换其他预览即可，无需逐项点读取'
        else:self.message='日期已识别；训练名称/收益待校准。也可用快速录入，不必等待模型'
        if idx is not None and 'goal' in confirmed and date.get('remaining') is not None:
            goal=confirmed['goal'];deadline=idx+date['remaining']
            if deadline<=71:
                record=dict(name=goal['text'],deadline=deadline,required_fans=goal['required'],done=False,source='local_ocr_twice')
                known=local.setdefault('goals',[])
                old=next((g for g in known if g['name']==record['name'] or (g['deadline']==record['deadline'] and g['required_fans']==record['required_fans'])),None)
                if old and old.get('source')!='user_confirmed':old.update(record)
                elif not old and len(known)<30:known.append(record)
        run.state_revision+=1;self.session.tracker.save()


def date_label(idx):
    return f'{["初級","古典","資深"][idx//24]}年{idx%24//2+1}月{"後半" if idx%2 else "前半"}'
