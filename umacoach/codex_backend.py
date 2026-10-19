"""Official Codex CLI backend using ChatGPT login, without handling OAuth tokens."""
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

from .guides import load_guide_pack
from .vision import PROMPT, validate


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT/'.runtime/codex'
VERSION = '0.159.2'
PACKAGE_URL = 'https://registry.npmjs.org/@openai/codex/-/codex-0.159.2-win32-x64.tgz'
# Official registry SRI, also checked against the npm ECDSA signature on the builder.
PACKAGE_SHA512 = '1ZJVTO40/ZaHPUUWc3uCX73jwzJRaDxAjRQEf37dC5Q3h1fp/Z7+1L8oX3bPlXgjhEY6kHW3rm0wz2sdQ5rxNw=='
EXE = Path('vendor/x86_64-pc-windows-msvc/bin/codex.exe')


class CodexError(RuntimeError):
    def __init__(self, message, reason='other'):
        super().__init__(message)
        self.reason = reason


def binary():
    path = INSTALL/EXE
    receipt = INSTALL/'verified-install.json'
    if path.is_file() and receipt.is_file():
        data = json.loads(receipt.read_text('utf-8'))
        if data.get('sha512') == PACKAGE_SHA512:
            return str(path)
    return shutil.which('codex.exe' if sys.platform == 'win32' else 'codex')


def cli_env():
    result = os.environ.copy()
    # Account mode must never silently fall back to API-key billing.
    for name in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN'):
        result.pop(name, None)
    return result


def install(progress):
    if sys.platform != 'win32':
        raise CodexError('自动安装仅支持 Windows 64位。')
    if binary():
        return
    INSTALL.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='codex-install-', dir=INSTALL.parent) as temp:
        temp = Path(temp)
        archive = temp/'package.tgz'
        digest, received = hashlib.sha512(), 0
        with urllib.request.urlopen(PACKAGE_URL, timeout=90) as response, archive.open('wb') as out:
            while chunk := response.read(1024*1024):
                received += len(chunk)
                if received > 200_000_000:
                    raise CodexError('官方组件下载大小异常，已停止安装。')
                digest.update(chunk)
                out.write(chunk)
                progress(f'正在下载官方 Codex 组件：{received//1_000_000} MB / 约162 MB（仅首次）')
        if base64.b64encode(digest.digest()).decode() != PACKAGE_SHA512:
            raise CodexError('Codex 官方组件校验失败，未执行该组件；请稍后重试。')
        progress('官方组件校验通过，正在解压…')
        staging = temp/'runtime'
        staging.mkdir()
        with tarfile.open(archive, 'r:gz') as tar:
            for member in tar:
                name = PurePosixPath(member.name)
                if name.is_absolute() or '..' in name.parts or '\\' in member.name or ':' in member.name or not member.name.startswith('package/'):
                    raise CodexError('官方组件压缩包路径异常。')
                if member.isdir():
                    continue
                if not member.isfile():
                    raise CodexError('官方组件包含不支持的文件类型。')
                target = staging/str(name.relative_to('package'))
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, target.open('wb') as dest:
                    shutil.copyfileobj(src, dest)
        if not (staging/EXE).is_file():
            raise CodexError('官方组件缺少 Codex 程序。')
        (staging/'verified-install.json').write_text(json.dumps(dict(version=VERSION, sha512=PACKAGE_SHA512, source=PACKAGE_URL)), 'utf-8')
        # Preserve any pre-existing files rather than overwriting unknown contents.
        if INSTALL.exists():
            raise CodexError('Codex 组件目录已有不完整内容，请将 .runtime/codex 文件夹改名后重试。')
        staging.replace(INSTALL)


def login_status():
    exe = binary()
    if not exe:
        return 'not_installed'
    try:
        p = subprocess.run([exe, '-c', 'forced_login_method="chatgpt"', 'login', 'status'],
                           capture_output=True, text=True, encoding='utf-8', errors='replace',
                           timeout=8, env=cli_env(), creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
    except (OSError, subprocess.TimeoutExpired):
        return 'unavailable'
    text = (p.stdout+'\n'+p.stderr).lower()
    return 'ready' if p.returncode == 0 and 'logged in using chatgpt' in text else 'login_required'


def open_login():
    exe = binary()
    if not exe:
        raise CodexError('请先安装官方 Codex 组件。', 'login')
    subprocess.Popen([exe, '-c', 'forced_login_method="chatgpt"', 'login'], env=cli_env(),
                     creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == 'win32' else 0)


def schema():
    strings = ('character', 'date_text', 'recommendation', 'reason')
    arrays = ('evidence', 'missing', 'alternatives', 'guide_refs')
    properties = {k: {'type': 'string'} for k in strings}
    properties.update({k: {'type': 'array', 'items': {'type': 'string'}} for k in arrays})
    properties.update(scene={'type': 'string', 'enum': ['unknown', 'title', 'run_start', 'training', 'event', 'race', 'skills', 'abandon_dialog', 'abandon_confirmed', 'result']},
                      confidence={'type': 'number', 'minimum': 0, 'maximum': 1},
                      turn_index={'type': ['integer', 'null'], 'minimum': 0, 'maximum': 95})
    return dict(type='object', additionalProperties=False, properties=properties, required=list(properties))


def analyze_codex(image, context, guide_path):
    if login_status() != 'ready':
        raise CodexError('尚未完成 ChatGPT 账号登录，请点“使用 ChatGPT 登录”，完成后检查登录状态。', 'login')
    guide, allowed = load_guide_pack(guide_path)
    temporary = ROOT/'local-data/codex-tmp'
    temporary.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temporary) as temp:
        folder = Path(temp)
        screenshot, definition, output = folder/'screen.jpg', folder/'schema.json', folder/'result.json'
        pic = image.convert('RGB')
        pic.thumbnail((1600, 1600))
        pic.save(screenshot, 'JPEG', quality=85)
        definition.write_text(json.dumps(schema()), 'utf-8')
        command = [binary(), 'exec', '--ignore-user-config', '--skip-git-repo-check', '--ephemeral',
                   '--sandbox', 'read-only', '-c', 'forced_login_method="chatgpt"',
                   '-c', 'history.persistence="none"', '-c', 'model_reasoning_effort="low"', '--color', 'never',
                   '--image', str(screenshot), '--output-schema', str(definition),
                   '-o', str(output), '-C', str(folder)]
        for feature in ('shell_tool', 'unified_exec', 'multi_agent', 'apps', 'plugins', 'hooks',
                        'browser_use', 'computer_use', 'image_generation', 'view_image', 'code_mode_host'):
            command += ['--disable', feature]
        command.append('-')
        prompt = PROMPT+'\n仅根据附图和以下资料分析，不使用工具、命令、其他文件或网络。建议和理由分别不超过120字，先给出下一步。\n'+json.dumps(context, ensure_ascii=False)+'\n'+guide
        try:
            p = subprocess.run(command, input=prompt, capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=180, env=cli_env(),
                               creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        except subprocess.TimeoutExpired:
            raise CodexError('Codex 本次分析超时，已暂停；可以稍后手动重试。', 'network') from None
        if p.returncode != 0 or not output.is_file():
            text = (p.stderr+p.stdout).lower()
            if any(word in text for word in ('usage limit', 'rate_limit_exceeded', 'limit reached')):
                raise CodexError('当前账号 Codex 额度或频率达到限制，请等待额度恢复。', 'quota')
            raise CodexError('Codex 分析未完成，请检查官方登录、网络和账号额度后重试。')
        try:
            result = validate(json.loads(output.read_text('utf-8')))
        except (ValueError, KeyError, TypeError):
            raise CodexError('Codex 返回的识别结果格式不完整，请重新分析。') from None
        if any(ref not in allowed for ref in result['guide_refs']):
            raise CodexError('Codex 引用了未核验的攻略，已拒绝本条建议。')
        return result
