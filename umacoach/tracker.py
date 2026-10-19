"""Conservative, persisted run boundaries; no score thresholds are assumed."""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import json
import uuid
from .run_state import empty_state, merge_state


@dataclass
class Run:
    id: str
    started_at: str
    character: str = ""
    last_turn: int | None = None
    status: str = "active"
    reason: str = ""
    state: dict = field(default_factory=empty_state)
    state_revision: int = 0
    local: dict = field(default_factory=dict)
    character_locked: bool = False


class Tracker:
    def __init__(self, path: Path):
        self.path = path
        self.runs: list[Run] = []
        self.pending = False
        # Persisted sessions need reconciliation after an app restart.
        self.last_scene = "unknown"
        if path.exists():
            self.runs = [Run(**r) for r in json.loads(path.read_text('utf-8'))]
            for run in self.runs:
                run.state = empty_state() | run.state
            self.pending = self.active is not None

    @property
    def active(self):
        return next((r for r in reversed(self.runs) if r.status == 'active'), None)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps([asdict(r) for r in self.runs], ensure_ascii=False, indent=2), 'utf-8')
        tmp.replace(self.path)

    def close(self, reason: str):
        if self.active:
            self.active.status = 'completed' if reason == 'result' else 'ended'
            self.runs[-1].reason = reason
        self.pending = False
        self.save()

    def new(self, character=''):
        if self.active:
            self.close('replaced_by_new_run')
        self.runs.append(Run(uuid.uuid4().hex[:12], datetime.now(timezone.utc).isoformat(), character))
        self.pending = False
        self.save()

    def resume(self):
        if not self.active:
            self.new()
        self.pending = False

    def apply(self, observation: dict) -> str:
        prior = self.active
        status = self._apply_boundary(observation)
        if observation['confidence'] >= .90 and not self.pending:
            target = self.active
            if observation['scene'] in ('result', 'abandon_confirmed'):
                target = prior
            if target and observation['scene'] in ('run_start', 'training', 'event', 'race', 'skills', 'result', 'abandon_confirmed'):
                state, changed = merge_state(target.state, observation.get('state_update'),
                    turn=observation.get('turn_index'), date_text=observation.get('date_text', ''))
                if changed:
                    target.state = state
                    target.state_revision += 1
                    self.save()
        return status

    def _apply_boundary(self, observation: dict) -> str:
        scene = observation['scene']
        confidence = observation['confidence']
        character = observation.get('character') or ''
        turn = observation.get('turn_index')
        if confidence < 0.85:
            return '画面识别不确定，局次未变更'
        if scene == 'run_start':
            if self.last_scene != 'run_start':
                self.new(character)
            self.last_scene = scene
            return '已识别养成开始'
        if scene in ('result', 'abandon_confirmed'):
            self.close(scene)
            self.last_scene = scene
            return '本局已结束' if scene == 'result' else '已识别放弃，本局结束'
        if scene == 'abandon_dialog':
            self.last_scene = scene
            return '正在询问放弃，尚未认定本局结束'
        if scene in ('training', 'event', 'race', 'skills'):
            if self.active is None:
                # A mid-run screen proves neither a start nor a restart boundary.
                self.pending = True
                self.last_scene = scene
                return '正在养成中：请选“记录为新局”或“继续当前局”确认归属'
            run = self.active
            changed_character = bool(not run.character_locked and character and run.character and character != run.character)
            rollback = turn is not None and run.last_turn is not None and turn < run.last_turn
            if changed_character or rollback:
                self.pending = True
            if self.pending:
                return '疑似新局或恢复会话，请确认局次；暂停建议以免混用记录'
            if character and not run.character_locked:
                run.character = character
            if turn is not None:
                run.last_turn = turn
            self.save()
        self.last_scene = scene
        return '当前局持续记录中' if self.active else '等待养成开始'
