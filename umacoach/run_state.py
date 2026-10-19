"""Validated, evidence-backed state patches; omitted values never mean zero."""
from copy import deepcopy
from datetime import datetime, timezone


MIN_CONFIDENCE = .90
MENTORS = ['未定', '聖烈特', '速度象徵', '俳成功']
MOODS = ['超絕好調', '絕好調', '好調', '普通', '不調', '絕不調']
COLORS = ['blue', 'green', 'pink']


class StateError(ValueError):
    pass


def integer(low, high):
    return dict(type='integer', minimum=low, maximum=high)


def nullable(value):
    return {'anyOf': [value, {'type': 'null'}]}


def obj(properties):
    # Codex structured output requires every property to be required. Null is
    # its "not visible / no update" spelling; API callers may omit properties.
    return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)


def string():
    return dict(type='string', minLength=1, maxLength=120)


INSIGHT = obj(dict(name=string(), color={'type': 'string', 'enum': COLORS}, stars=integer(1, 3)))
RACE = obj(dict(name=string(), date_text=string(), place=nullable(integer(1, 18)), grade=nullable(string())))
FEST = obj(dict(stage={'type': 'string', 'enum': ['Stella', 'Pride', 'Legend']},
                date_text=string(), place=nullable(integer(1, 18))))
VALUES = dict(
    fans=integer(0, 99_999_999),
    attributes=obj({key:nullable(integer(0,2500)) for key in ('speed','stamina','power','guts','wisdom')}),
    mentor={'type': 'string', 'enum': MENTORS},
    insights=dict(type='array', items=INSIGHT, maxItems=10),
    insight_replacements=dict(type='array', maxItems=10,
        items=obj(dict(old_name=string(), new_insight=INSIGHT))),
    guide_gauges=obj({color: nullable(integer(0, 8)) for color in COLORS}),
    turns_to_insight=integer(0, 6),
    outings_used=integer(0, 5),
    friend_levels=dict(type='array', maxItems=20, items=obj(dict(name=string(), level={
        'anyOf': [integer(0, 99), {'type': 'string', 'enum': ['MAX']}]}))),
    energy=obj(dict(current=nullable(integer(0, 200)), maximum=nullable(integer(1, 200)))),
    mood={'type': 'string', 'enum': MOODS},
    races=dict(type='array', items=RACE, maxItems=200),
    race_totals=obj(dict(starts=nullable(integer(0, 200)), wins=nullable(integer(0, 200)))),
    dream_fest=dict(type='array', items=FEST, maxItems=3))


def update_schema():
    return nullable(obj({key: nullable(obj(dict(value=nullable(value),
        confidence={'type': 'number', 'minimum': 0, 'maximum': 1}, evidence=string())))
        for key, value in VALUES.items()}))


def _validate(value, spec, label):
    if 'anyOf' in spec:
        for candidate in spec['anyOf']:
            try:
                _validate(value, candidate, label)
                return
            except StateError:
                pass
        raise StateError(label+'的值或类型无效')
    kind = spec['type']
    ok = {'null': lambda: value is None,
          'integer': lambda: type(value) is int,
          'number': lambda: type(value) in (int, float),
          'string': lambda: isinstance(value, str),
          'array': lambda: isinstance(value, list),
          'object': lambda: isinstance(value, dict)}[kind]()
    if not ok:
        raise StateError(label+'的类型无效')
    if 'enum' in spec and value not in spec['enum']:
        raise StateError(label+'的取值无效')
    if kind in ('integer', 'number') and not spec.get('minimum', value) <= value <= spec.get('maximum', value):
        raise StateError(label+'超出允许范围')
    if kind == 'string' and not spec.get('minLength', 0) <= len(value.strip()) <= spec.get('maxLength', 10000):
        raise StateError(label+'不能为空或过长')
    if kind == 'array':
        if len(value) > spec.get('maxItems', 200):
            raise StateError(label+'条目过多')
        for item in value:
            _validate(item, spec['items'], label)
    if kind == 'object':
        props = spec['properties']
        if set(value)-set(props) or set(spec.get('required', []))-set(value):
            raise StateError(label+'字段不完整或存在未知字段')
        for key, item in value.items():
            _validate(item, props[key], label+'.'+key)


def validate_update(update):
    if update is None:
        return
    if not isinstance(update, dict) or set(update)-set(VALUES):
        raise StateError('state_update字段无效')
    for key, envelope in update.items():
        if envelope is None:
            continue
        _validate(envelope, obj(dict(value=nullable(VALUES[key]),
            confidence={'type': 'number', 'minimum': 0, 'maximum': 1}, evidence=string())), key)


def empty_state():
    return dict(fans=None, attributes={k:None for k in ('speed','stamina','power','guts','wisdom')}, mentor='未定', insights=None, guide_gauges={c: None for c in COLORS},
        turns_to_insight=None, outings_used=None, friend_levels=None,
        energy=dict(current=None, maximum=None), mood=None, races=None,
        race_totals=dict(starts=None, wins=None), dream_fest=None,
        replacements=[], evidence={})


def _unique(items, key, label):
    keys = [key(item) for item in items]
    if len(keys) != len(set(keys)):
        raise StateError(label+'含重复条目')


def _upsert(old, new, key):
    result = deepcopy(old or [])
    for item in new:
        found = next((i for i, existing in enumerate(result) if key(existing) == key(item)), None)
        if found is None:
            result.append(deepcopy(item))
        else:
            # A visible race list without a placing must not erase its result.
            result[found].update({k: v for k, v in item.items() if v is not None})
    return result


def _check(state):
    insights = state['insights'] or []
    if len(insights) > 10:
        raise StateError('心得最多10个；请提供明确的替换记录，不能凭部分截图删除旧心得')
    _unique(insights, lambda x: x['name'], '心得')
    _unique(state['friend_levels'] or [], lambda x: x['name'], '亲友等级')
    _unique(state['races'] or [], lambda x: (x['date_text'], x['name']), '比赛')
    _unique(state['dream_fest'] or [], lambda x: x['stage'], 'Dream Fest')
    if len(state['races'] or []) > 200 or len(state['friend_levels'] or []) > 20:
        raise StateError('记录条目过多')
    energy, totals = state['energy'], state['race_totals']
    if energy['current'] is not None and energy['maximum'] is not None and energy['current'] > energy['maximum']:
        raise StateError('体力不能超过体力上限')
    if totals['starts'] is not None and totals['wins'] is not None and totals['wins'] > totals['starts']:
        raise StateError('胜场不能超过出赛场次')


def merge_state(state, update, *, manual=False, turn=None, date_text=''):
    """Return a new state atomically. Model lists are visible subsets, not totals."""
    if manual:
        if not isinstance(update, dict) or set(update)-(set(VALUES)-{'insight_replacements'}):
            raise StateError('手动状态字段无效')
        for key, value in update.items():
            _validate(value, nullable(VALUES[key]), key)
    else:
        validate_update(update)
        update = update or {}
    result = deepcopy(state)
    before = deepcopy(state['insights'])
    stamp = datetime.now(timezone.utc).isoformat()
    changed = False
    # Explicit replacements precede additions, so a full list never grows to 11.
    keys = sorted(update, key=lambda k: k != 'insight_replacements')
    for key in keys:
        envelope = update[key]
        if not manual and (envelope is None or envelope['value'] is None or envelope['confidence'] < MIN_CONFIDENCE):
            continue
        value = deepcopy(envelope if manual else envelope['value'])
        if key == 'insight_replacements':
            for replacement in value:
                old_name, new = replacement['old_name'], replacement['new_insight']
                items = result['insights'] or []
                index = next((i for i, item in enumerate(items) if item['name'] == old_name), None)
                if index is None:
                    if new not in items:
                        raise StateError('替换的旧心得未记录，请先核对本局心得')
                    continue  # same screen analyzed again
                items[index] = new
                result['insights'] = items
        elif not manual and key in ('guide_gauges', 'energy', 'race_totals','attributes'):
            result[key].update({k: v for k, v in value.items() if v is not None})
        elif not manual and key in ('insights', 'friend_levels', 'races', 'dream_fest'):
            identity = (lambda x: (x['date_text'], x['name'])) if key == 'races' else (
                (lambda x: x['stage']) if key == 'dream_fest' else (lambda x: x['name']))
            _unique(value, identity, key)
            result[key] = _upsert(result[key], value, identity)
        else:
            result[key] = (empty_state()[key] if value is None else value)
        # Null components of partial observations did not confirm those fields.
        metadata = dict(source='manual' if manual else 'model',
            confidence=1.0 if manual else envelope['confidence'],
            evidence='用户手动修正' if manual else envelope['evidence'],
            turn_index=turn, date_text=date_text, updated_at=stamp)
        if key in ('guide_gauges', 'energy', 'race_totals','attributes') and value is not None:
            for component, observed in value.items():
                if manual or observed is not None:
                    result['evidence'][key+'.'+component] = metadata
        elif key in ('insights', 'friend_levels', 'races', 'dream_fest') and value is not None:
            for item in value:
                identity = item['stage'] if key == 'dream_fest' else item['name']
                if key == 'races':
                    identity = item['date_text']+' / '+identity
                result['evidence'][key+'.'+identity] = metadata
        else:
            result['evidence'][key] = metadata
        changed = True
    _check(result)
    removed = [i for i in before or [] if i['name'] not in {x['name'] for x in result['insights'] or []}]
    if removed:
        added = [i for i in result['insights'] or [] if i['name'] not in {x['name'] for x in before or []}]
        result['replacements'].append(dict(removed=removed, added=added, source='manual' if manual else 'model',
            turn_index=turn, date_text=date_text, updated_at=stamp))
    return result, changed


def context_state(state):
    result = deepcopy(state)
    result['observed_race_counts'] = dict(starts=len(state['races'] or []),
        wins=sum(item['place'] == 1 for item in state['races'] or []))
    result['notice'] = ('null表示未知。races及observed_race_counts只统计已记录比赛，不能当作整局总数。'
        '状态可能是旧回合的观测；参考evidence中的日期与回合，体力、干劲、指南条必须以当前画面为准。')
    return result
