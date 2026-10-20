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
    for key in ('friends','participants','unbonded','hints'):
        n=value.get(key)
        if n is not None and (type(n) is not int or not 0<=n<=6):raise ValueError('支援人数应为0到6，未知留空')
    if value.get('color') not in (None,'blue','green','pink'):raise ValueError('指南条颜色无效')
    gauges=value.get('guide_gauges')
    if gauges is not None and (not isinstance(gauges,dict) or set(gauges)-{'blue','green','pink'} or any(n is not None and (type(n) is not int or not 0<=n<=8) for n in gauges.values())):
        raise ValueError('指南条格数应为0到8，未知留空')
    return {key:value.get(key) for key in ('kind','gains','failure','friends','participants','unbonded','hints','color','guide_gauges')}


def score_preview(preview,state,index,config=None):
    from .scoring import Weights,score
    return score(preview,state,index,config or Weights().config)


def ranking(cache,state,index,risk_limit=15,now=None,config=None,live=None):
    from .scoring import Weights,recovery
    config=config or Weights().config
    now=time.time() if now is None else now
    fresh={key:item for key,item in cache.items() if now-item['at']<=120}
    rows=sorted([score_preview(item['preview'],state,index,config) for item in fresh.values()],key=lambda row:row['score'],reverse=True)
    safe=[row for row in rows if row['failure']<=risk_limit]
    missing=[kind for kind in TYPES if kind not in fresh]
    if safe:
        best=safe[0]
        action=f'已见训练中优先点「{best["kind"]}」；失败率{best["failure"]}%。'
    elif rows:
        action=f'已见训练均超过失败率上限{risk_limit}%，先查看智力训练、休息或外出。'
    else:action='尚无可比较的本回合完整收益。请切到速度训练预览，持续读取会自动记录；不需再点读取。'
    rest=recovery(state,live or {},config)
    recovery_choice=rest[0] if rest and (not safe or rest[0]['score']>safe[0]['score']) else None
    if recovery_choice:action=recovery_choice['text']
    if not missing and safe and not recovery_choice:action=f'本回合选「{safe[0]["kind"]}」训练；五项已见，按当前权重及风险排序。'
    if missing and rows:action+=' 如需补全比较，下一项查看「'+missing[0]+'」。'
    return dict(rows=rows,recovery=rest,action=recovery_choice['action'] if recovery_choice else 'training' if safe else 'inspect',missing=missing,next_preview=missing[0] if missing else None,recommendation=action,
        scope='全部五项已见（本地经验规则）' if not missing else '仅比较已见项目，不代表全局最优',
        rules='权重是本程序的可解释启发式，参考J01-8/J02-5（跨服推定）；分数不是游戏评价分。未识别的人数/颜色不计加分。')
