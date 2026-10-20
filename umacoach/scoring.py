"""Validated local scoring policy, editable JSON only (never evaluated code)."""
from copy import deepcopy
import json
import math
from pathlib import Path

KEYS=['speed','stamina','power','guts','wisdom']


def validate_weights(config):
    baseline=json.loads((Path(__file__).resolve().parents[1]/'guides/training_weights.json').read_text('utf-8'))
    if not isinstance(config,dict) or set(config)!=set(baseline) or type(config.get('version')) is not int or config.get('version')!=1:raise ValueError('评分配置版本/字段不完整')
    if not isinstance(config['notice'],str) or len(config['notice'])>1000:raise ValueError('评分说明无效')
    def number(v,nullable=False):
        if nullable and v is None:return
        if type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=10000:raise ValueError('评分权重必须为0到10000的有限数值')
    for key,value in config.items():
        if key not in ('version','notice','phases','attribute_caps','outing_below_mood'):number(value)
    if config['rest_below_percent']>100:raise ValueError('休息阈值应为百分比')
    if config['outing_below_mood'] not in ('絕不調','不調','普通','好調','絕好調','超絕好調'):raise ValueError('干劲阈值无效')
    if not isinstance(config['attribute_caps'],list) or len(config['attribute_caps'])!=5:raise ValueError('属性上限需五项')
    for v in config['attribute_caps']:number(v,True)
    if not isinstance(config['phases'],dict) or set(config['phases'])!={'early','classic','senior'}:raise ValueError('阶段配置不完整')
    for phase in config['phases'].values():
        if not isinstance(phase,dict) or set(phase)!={'stat_weights','targets','skill_weight','bond_weight'}:raise ValueError('阶段权重字段不完整')
        for key in ('stat_weights','targets'):
            if not isinstance(phase[key],list) or len(phase[key])!=5:raise ValueError('阶段属性需要五项')
            for v in phase[key]:number(v,key=='targets')
        number(phase['skill_weight']);number(phase['bond_weight'])
    return deepcopy(config)


class Weights:
    def __init__(self,path=None):
        self.path=Path(path) if path else None
        self.error=None
        self.config=json.loads((Path(__file__).resolve().parents[1]/'guides/training_weights.json').read_text('utf-8'))
        if self.path and self.path.exists():
            try:self.config=validate_weights(json.loads(self.path.read_text('utf-8')))
            except (ValueError,TypeError,KeyError):self.error='本地评分配置无效，当前采用默认权重；请检查后重新保存'

    def save(self,config):
        checked=validate_weights(config)
        if self.path:
            self.path.parent.mkdir(parents=True,exist_ok=True);tmp=self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(checked,ensure_ascii=False,indent=2),'utf-8');tmp.replace(self.path)
        self.config=checked;self.error=None


def score(preview,state,index,config):
    early=index is None or index<24
    phase=config['phases']['early' if early else 'classic' if index<48 else 'senior']
    stats=state.get('attributes',{});visible=0;effective=[]
    for i,key in enumerate(KEYS):
        gain=preview['gains'][i];current=stats.get(key);cap=config['attribute_caps'][i];target=phase['targets'][i]
        evidence=state.get('evidence',{})
        records=[evidence[k] for k in ('attributes','attributes.'+key) if k in evidence]
        if records and not any(record.get('turn_index')==index for record in records):current=None
        if cap is not None and current is not None:gain=min(gain,max(0,cap-current))
        weight=phase['stat_weights'][i]
        if target and current is not None:weight*=1+config['target_deficit_weight']*max(0,target-current)/target
        visible+=gain*weight;effective.append(gain)
    visible+=preview['gains'][5]*phase['skill_weight']
    bond=(preview.get('unbonded') or 0)*phase['bond_weight']
    friends=(preview.get('friends') or 0)*config['friendship_weight']
    people=(preview.get('participants') or 0)*config['participants_weight']
    hint=(preview.get('hints') or 0)*config['hint_weight']
    progress=0;color=preview.get('color');gauges=preview.get('guide_gauges') or state.get('guide_gauges',{})
    if color:
        current=gauges.get(color)
        if current is not None:
            addition=3 if (preview.get('friends') or 0)>0 else 1
            after=min(8,current+addition)
            progress=(after-current)*config['guide_step_weight']+(config['guide_full_bonus'] if current<8 and after==8 else 0)
        if color=='pink' and (index is None or index<36) and state.get('mentor') in ('未定',None):progress+=config['pink_route_bonus']
        if color in ('pink','green') and index is not None and index>=41:progress+=config['late_pink_green_bonus']
    summer=index is not None and index>=24 and index%24 in (12,13,14,15)
    multiplier=config['summer_multiplier'] if summer else 1
    p=preview['failure']/100
    result=(visible+bond+friends+people+hint+progress)*multiplier*(1-p)-p*config['failure_expected_loss']
    missing=[label for key,label in [('unbonded','未满羁绊人数'),('friends','友情人数'),('participants','在场支援人数'),('color','碎片颜色'),('hints','提示图标')] if preview.get(key) is None]
    return dict(kind=preview['kind'],score=round(result,2),failure=preview['failure'],gains=preview['gains'],effective_gains=effective,
        explanation=f'属性/Pt {visible:.1f}＋羁绊{bond:.1f}＋友情{friends:.1f}＋人数{people:.1f}＋提示{hint:.1f}＋指南{progress:.1f}；'+
          ('夏合宿权重；' if summer else '')+f'成功概率×收益－失败概率×损失{config["failure_expected_loss"]}',missing=missing)


def recovery(state,live,config):
    energy=live.get('energy_percent');mood=live.get('mood');ailment=live.get('ailment')
    candidates=[]
    if ailment is True:candidates.append(dict(action='infirmary',score=config['infirmary_priority'],text='回主画面优先去医务室，已确认可治疗异常；先核对按钮可用。'))
    if energy is not None and energy<config['rest_below_percent']:
        candidates.append(dict(action='rest',score=config['rest_priority']+(config['rest_below_percent']-energy),text=f'回主画面优先休息：体力条约{energy}%，低于配置阈值。'))
    moods=['絕不調','不調','普通','好調','絕好調','超絕好調']
    if mood in moods and moods.index(mood)<=moods.index(config['outing_below_mood']):
        candidates.append(dict(action='outing',score=config['outing_priority'],text='回主画面优先外出改善干劲；团队卡外出次数和选项另行核对。'))
    return sorted(candidates,key=lambda row:row['score'],reverse=True)
