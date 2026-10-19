"""Transparent local heuristics, never an evaluation-score or optimality claim."""
import time
from .local_ocr import TYPES


def validate_preview(value):
    if not isinstance(value,dict) or value.get('kind') not in TYPES:raise ValueError('请选择训练项目')
    gains=value.get('gains')
    if not isinstance(gains,list) or len(gains)!=6 or any(type(n) is not int or not 0<=n<=300 for n in gains):
        raise ValueError('请确认完整的五维收益和技能Pt，未知不能填成0')
    failure=value.get('failure')
    if type(failure) is not int or not 0<=failure<=100:raise ValueError('请确认0到100的失败率')
    for key in ('friends','participants','unbonded'):
        n=value.get(key)
        if n is not None and (type(n) is not int or not 0<=n<=6):raise ValueError('支援人数应为0到6，未知留空')
    if value.get('color') not in (None,'blue','green','pink'):raise ValueError('指南条颜色无效')
    return {key:value.get(key) for key in ('kind','gains','failure','friends','participants','unbonded','color')}


def score_preview(preview,state,index):
    early=index is None or index<24
    weights=[1,1,1,1,1,.55 if early else 1.5]
    visible=sum(n*w for n,w in zip(preview['gains'],weights))
    bond=(preview.get('unbonded') or 0)*(4 if early else 1)
    friendship=(preview.get('friends') or 0)*1.5
    people=(preview.get('participants') or 0)*.3
    progress=0
    color=preview.get('color')
    if color:
        current=state.get('guide_gauges',{}).get(color)
        if current is not None:
            addition=3 if (preview.get('friends') or 0)>0 else 1
            # Milestones, not a claim about their expected rating points.
            after=min(8,current+addition)
            progress=(after-current)*.8+(5 if current<8 and after==8 else 0)
    total=(visible+bond+friendship+people+progress)*(1-preview['failure']/100)-preview['failure']*.8
    missing=[label for key,label in [('unbonded','未满羁绊人数'),('friends','友情人数'),('participants','在场支援人数'),('color','指南条颜色')] if preview.get(key) is None]
    return dict(kind=preview['kind'],score=round(total,2),failure=preview['failure'],gains=preview['gains'],
                explanation=f'收益权重和{visible:.1f}＋羁绊{bond:.1f}＋友情{friendship:.1f}＋人数{people:.1f}＋指南{progress:.1f}，再扣失败风险',missing=missing)


def ranking(cache,state,index,risk_limit=15,now=None):
    now=time.time() if now is None else now
    fresh={key:item for key,item in cache.items() if now-item['at']<=120}
    rows=sorted([score_preview(item['preview'],state,index) for item in fresh.values()],key=lambda row:row['score'],reverse=True)
    safe=[row for row in rows if row['failure']<=risk_limit]
    missing=[kind for kind in TYPES if kind not in fresh]
    if safe:
        best=safe[0]
        action=f'已见训练中优先点「{best["kind"]}」；失败率{best["failure"]}%。'
    elif rows:
        action=f'已见训练均超过失败率上限{risk_limit}%，先查看智力训练、休息或外出。'
    else:action='尚无可比较的本回合完整收益。请切到速度训练预览，持续读取会自动记录；不需再点读取。'
    if missing and rows:action+=' 如需补全比较，下一项查看「'+missing[0]+'」。'
    return dict(rows=rows,missing=missing,next_preview=missing[0] if missing else None,recommendation=action,
        scope='全部五项已见（本地经验规则）' if not missing else '仅比较已见项目，不代表全局最优',
        rules='权重是本程序的可解释启发式，参考J01-8/J02-5（跨服推定）；分数不是游戏评价分。未识别的人数/颜色不计加分。')
