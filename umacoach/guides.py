"""Select relevant rules while keeping cross-server uncertainty explicit."""
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
                and source.get('origin_server') in ('jp', 'tw')
                and source.get('tw_applicability') in ('verified', 'assumed')
                and source.get('scenario') == 'legendary'
                and source.get('goal') == 'rating'
                and source.get('rules')):
            entry = dict(source)
            entry['applicability_label'] = ('跨服推定、仅供参考；引用时说明来源，不得称为最优或公认'
                if source['tw_applicability'] == 'assumed' else '已核验繁中服适用性')
            eligible.append(entry)
    pack = {'workflow': {'server': 'tw', 'scenario': 'legendary', 'goal': 'rating'},
            'eligible_sources': eligible,
            'notice': '仅可引用eligible_sources的来源ID。assumed是跨服推定、仅供参考；【有争议】【作者经验】必须保留限定，不得当作确定机制或最优结论。'}
    return notes+'\n\n按适用性分级的参考规则包：\n'+json.dumps(pack, ensure_ascii=False), {s['id'] for s in eligible}
