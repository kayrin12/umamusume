"""Incomplete sourced schedules never imply that a run is safe to continue."""
import json
from pathlib import Path


def catalog():
    return json.loads((Path(__file__).resolve().parents[1]/'guides/character_goals.json').read_text('utf-8'))


def profile_options():
    return [dict(id=p['id'],name=p['name'],name_jp=p['name_jp'],variant=p['variant']) for p in catalog()['profiles']]


def find_profile(identifier):
    return next((p for p in catalog()['profiles'] if p['id']==identifier),None)


def alerts(run):
    data=catalog();local=run.local;index=local.get('date',{}).get('calendar_index')
    profile=find_profile(local.get('profile_id'))
    refs=[]
    if profile:
        refs=[g for g in profile['goals'] if index is None or g['calendar_index']>=index][:3]
    goals=[g for g in local.get('goals',[]) if not g.get('done') and (index is None or g['deadline']>=index)]
    goals.sort(key=lambda g:g['deadline'])
    messages=[]
    if not run.character:messages.append('请先固定本局角色，避免反复识别；可从列表选择或填写繁中名称。')
    fans=run.state.get('fans')
    if fans is None:messages.append('当前粉丝数未知，不能判断是否满足目标/报名条件；请打开粉丝区域或手动填写。')
    if not goals:messages.append('当前没有已确认的粉丝门槛；参考赛程不含完整门槛，请从客户端目标/报名条件登记。')
    urgent=False
    for goal in goals:
        remaining=goal['deadline']-index if index is not None else None
        needed=max(0,goal['required_fans']-fans) if fans is not None else None
        if fans is None and remaining is not None and remaining<=4:
            urgent=True;messages.append(f'{goal["name"]}距今{remaining}个日期回合，当前粉丝未知；请先核对门槛，不能判断安全。')
        elif needed and remaining is not None and remaining<=4:
            urgent=True;messages.append(f'优先处理粉丝门槛：{goal["name"]}距今{remaining}个日期回合，仍差{needed}粉丝；先查看可报名赛事，避免错过期限。')
        elif needed:messages.append(f'{goal["name"]}仍差{needed}粉丝，请提前预留比赛回合。')
        elif fans is not None:messages.append(f'{goal["name"]}的已登记粉丝门槛满足；其他出赛条件仍须检查。')
    for goal in local.get('goals',[]):
        if index is not None and not goal.get('done') and goal['deadline']<index:
            urgent=True
            messages.append(f'{goal["name"]}的登记期限已过；结果未确认，不能视为已完成。')
    candidates=[]
    if profile and index is not None and goals:
        until=min(goals[0]['deadline'],index+4)
        for race in data['races']:
            distance=race['distance']
            category='shortDistance' if distance<=1400 else 'mile' if distance<=1800 else 'mediumDistance' if distance<=2400 else 'longDistance'
            if index<=race['calendar_index']<=until and profile['affinities'].get(race['ground']) in ('A','B') and profile['affinities'].get(category) in ('A','B'):
                candidates.append(race)
        candidates.sort(key=lambda r:(r['calendar_index'],r['grade']!='G1'))
    return dict(urgent=urgent,messages=messages,reference_goals=refs,candidates=candidates[:8],
        source=data['source'],notice='参考赛程为日服原生角色目标，繁中服传奇杯未核验；不强制套用。候选赛事只按日期和基础适性筛选，报名粉丝、变体适性、获胜和收益均须在客户端确认。')
