"""Only pass rules with confirmed server/scenario/objective applicability."""
import json
from pathlib import Path


def load_guide_pack(path: Path):
    notes = path.read_text('utf-8') if path.exists() else '暂无已核验攻略。'
    registry = path.with_name('sources.json')
    sources = json.loads(registry.read_text('utf-8')) if registry.exists() else []
    eligible = []
    for source in sources:
        if (source.get('status') == 'verified'
                and source.get('server') == 'tw'
                and source.get('scenario') == 'legendary'
                and source.get('goal') == 'rating'
                and source.get('rules')):
            eligible.append(source)
    pack = {'workflow': {'server': 'tw', 'scenario': 'legendary', 'goal': 'rating'},
            'eligible_sources': eligible,
            'notice': '仅可引用eligible_sources的规则。未核验资料及历史赛事不得补全为当前规则。'}
    return notes+'\n\n已核验且适用的规则包：\n'+json.dumps(pack, ensure_ascii=False), {s['id'] for s in eligible}
