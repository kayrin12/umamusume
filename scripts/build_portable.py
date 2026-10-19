"""Build Windows x64 portable ZIP from PSF NuGet + PyPI packages.

Run on the Linux builder with Python, OpenSSL and network access. No Wine needed.
"""
import base64
import ast
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT/'local-data/portable-build'
OUTPUT = ROOT.parent/'artifacts/umacoach-windows-portable-x64.zip'
PYTHON = '3.13.16'
PILLOW = '12.3.0'


def fetch(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read()
    return gzip.decompress(data) if data[:2] == b'\x1f\x8b' else data


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name:
        raise ValueError('Unsafe archive entry')
    return str(path)


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # Verify the pinned official CLI package metadata signature before shipping
    # an installer that downloads that exact hash on the user's Windows PC.
    tree = ast.parse((ROOT/'umacoach/codex_backend.py').read_text('utf-8'))
    constants = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
                 if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                 and node.targets[0].id in ('VERSION', 'PACKAGE_SHA512')}
    cli = json.loads(fetch(f'https://registry.npmjs.org/@openai/codex/{constants["VERSION"]}-win32-x64'))
    if cli['name'] != '@openai/codex' or cli['dist']['integrity'] != 'sha512-'+constants['PACKAGE_SHA512']:
        raise ValueError('Unexpected pinned Codex package identity or hash')
    keys = json.loads(fetch('https://registry.npmjs.org/-/npm/v1/keys'))['keys']
    message = f'{cli["name"]}@{cli["version"]}:{cli["dist"]["integrity"]}'
    (CACHE/'npm-signature-message').write_text(message, 'utf-8')
    verified = False
    for signature in cli['dist']['signatures']:
        key = next((key for key in keys if key['keyid'] == signature['keyid']), None)
        if not key:
            continue
        encoded = key['key']
        pem = '-----BEGIN PUBLIC KEY-----\n'+'\n'.join(encoded[n:n+64] for n in range(0, len(encoded), 64))+'\n-----END PUBLIC KEY-----\n'
        (CACHE/'npm-key.pem').write_text(pem, 'utf-8')
        (CACHE/'npm-signature.bin').write_bytes(base64.b64decode(signature['sig'], validate=True))
        result = subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(CACHE/'npm-key.pem'),
                                 '-signature', str(CACHE/'npm-signature.bin'), str(CACHE/'npm-signature-message')], capture_output=True)
        if result.returncode == 0:
            verified = True
            break
    if not verified:
        raise ValueError('Codex npm registry signature verification failed')
    (ROOT/'umacoach/codex_provenance.json').write_text(json.dumps(dict(name=cli['name'], version=cli['version'],
        url=cli['dist']['tarball'], integrity=cli['dist']['integrity'], signature_keyid=signature['keyid'],
        npm_ecdsa_signature_verified=True), indent=2), 'utf-8')
    (ROOT/'umacoach/CODEX_LICENSE.txt').write_bytes(fetch(f'https://raw.githubusercontent.com/openai/codex/rust-v{constants["VERSION"]}/LICENSE'))
    print('Official Codex package signature verified; first-use installer hash pinned.')
    registration = json.loads(fetch(f'https://api.nuget.org/v3/registration5-gz-semver2/python/{PYTHON}.json'))
    catalog = json.loads(fetch(registration['catalogEntry']))
    if catalog['authors'] != 'Python Software Foundation' or catalog['version'] != PYTHON:
        raise ValueError('Unexpected Python package identity')
    package_path = CACHE/'python.nupkg'
    if not package_path.exists():
        package_path.write_bytes(fetch(registration['packageContent']))
    package = package_path.read_bytes()
    if catalog['packageHashAlgorithm'] != 'SHA512' or base64.b64encode(hashlib.sha512(package).digest()).decode() != catalog['packageHash']:
        raise ValueError('Python package checksum mismatch; refusing to build')
    if not shutil.which('openssl'):
        raise RuntimeError('OpenSSL is required to verify the NuGet CMS signature')
    with zipfile.ZipFile(package_path) as python_zip:
        signature = python_zip.read('.signature.p7s')
        (CACHE/'signature.p7s').write_bytes(signature)
        subprocess.run(['openssl', 'cms', '-verify', '-inform', 'DER', '-in', str(CACHE/'signature.p7s'),
                        '-purpose', 'any', '-out', str(CACHE/'nuget-signed-content.txt')], check=True)
        exe = python_zip.read('tools/python.exe')
        offset = struct.unpack_from('<I', exe, 0x3c)[0]
        if exe[:2] != b'MZ' or exe[offset:offset+4] != b'PE\0\0' or struct.unpack_from('<H', exe, offset+4)[0] != 0x8664:
            raise ValueError('Expected a Windows x64 Python executable')
        pypi = json.loads(fetch(f'https://pypi.org/pypi/Pillow/{PILLOW}/json'))
        wheel_info = next(f for f in pypi['urls'] if f['filename'] == f'pillow-{PILLOW}-cp313-cp313-win_amd64.whl')
        wheel_path = CACHE/wheel_info['filename']
        if not wheel_path.exists():
            wheel_path.write_bytes(fetch(wheel_info['url']))
        if hashlib.sha256(wheel_path.read_bytes()).hexdigest() != wheel_info['digests']['sha256']:
            raise ValueError('Pillow wheel checksum mismatch; refusing to build')
        provenance = {
            'python': {'version': PYTHON, 'publisher': catalog['authors'], 'url': registration['packageContent'],
                       'sha512_base64': catalog['packageHash'], 'checks': ['NuGet catalog SHA512', 'CMS signature and certificate chain']},
            'pillow': {'version': PILLOW, 'url': wheel_info['url'], 'sha256': wheel_info['digests']['sha256']},
            'target': 'Windows x64',
            'note': 'Built on Linux; Windows executable startup and capture still require Windows acceptance.'}
        with zipfile.ZipFile(OUTPUT, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            prefix = 'umacoach/'
            for info in python_zip.infolist():
                if not info.is_dir() and info.filename.startswith('tools/'):
                    name = safe_name(info.filename.removeprefix('tools/'))
                    bundle.writestr(prefix+'.runtime/python/'+name, python_zip.read(info))
            with zipfile.ZipFile(wheel_path) as wheel:
                for info in wheel.infolist():
                    if not info.is_dir():
                        bundle.writestr(prefix+'.runtime/python/Lib/site-packages/'+safe_name(info.filename), wheel.read(info))
            for folder in ('umacoach', 'guides'):
                for path in sorted((ROOT/folder).rglob('*')):
                    if path.is_file() and '__pycache__' not in path.parts:
                        bundle.write(path, prefix+str(path.relative_to(ROOT)))
            for name in ('start.bat', 'README.md', 'PORTABLE_README.txt'):
                data = (ROOT/name).read_bytes()
                if name.endswith(('.bat', '.txt')):
                    data = data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
                bundle.writestr(prefix+name, data)
            bundle.writestr(prefix+'.runtime/provenance.json', json.dumps(provenance, ensure_ascii=False, indent=2))
            bundle.writestr(prefix+'.runtime/python-package-signature.p7s', signature)
        with zipfile.ZipFile(OUTPUT) as bundle:
            if bundle.testzip() is not None:
                raise ValueError('Generated bundle failed integrity check')
            names = bundle.namelist()
            for required in ('start.bat', 'umacoach/webapp.py', 'umacoach/panel.html',
                             '.runtime/python/python.exe', '.runtime/python/vcruntime140.dll',
                             '.runtime/python/Lib/site-packages/PIL/_imaging.cp313-win_amd64.pyd'):
                if prefix+required not in names:
                    raise ValueError('Missing bundled component: '+required)
            if len(names) != len(set(names)):
                raise ValueError('Duplicate bundle paths')
        print('Portable bundle:', OUTPUT)
        print('Size:', OUTPUT.stat().st_size, 'bytes')
        print('SHA256:', hashlib.sha256(OUTPUT.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
