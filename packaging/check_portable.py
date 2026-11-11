#!/usr/bin/env python3
"""Exercise a release ZIP in a new relocated sandbox, preserving logs."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(path):
    return {p.relative_to(path).as_posix(): digest(p) for p in sorted(path.rglob('*')) if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output).absolute()
    output.mkdir(parents=True, exist_ok=False)
    checks = []
    def check(name, condition):
        checks.append({'name': name, 'passed': bool(condition)})
        if not condition:
            raise AssertionError(name)
    def run(name, cmd, code=0):
        proc = subprocess.run([sys.executable, '-B', '-X', 'utf8'] + [str(v) for v in cmd],
                              cwd=str(output), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              encoding='utf-8', timeout=120)
        (output / (name + '.json')).write_text(json.dumps({'command': [str(x) for x in cmd],
            'exit_code': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr},
            ensure_ascii=False, indent=2), encoding='utf-8')
        check(name, proc.returncode == code)
        return proc
    try:
        extracted = output / '移动 发布包'
        with zipfile.ZipFile(args.archive) as z:
            for name in z.namelist():
                # Test input produced by our builder; reject unexpected escaping members.
                p = Path(name)
                if p.is_absolute() or '..' in p.parts or ':' in name or '\\' in name:
                    raise ValueError('Unsafe ZIP member')
            z.extractall(str(extracted))
        bundle, = [p for p in extracted.iterdir() if p.is_dir()]
        installer = bundle / 'install.py'
        before = tree(bundle)
        run('verify_relocated', [installer, 'verify'])
        destination = output / '新 用户 skills'
        run('install_new', [installer, 'install', '--skills-dir', destination])
        skill = destination / 'adaptive-project'
        check('installed_bytes_match', tree(skill) == tree(bundle / 'adaptive-project'))
        installed = tree(skill)
        run('install_idempotent', [installer, 'install', '--skills-dir', destination])
        check('idempotent_bytes_unchanged', tree(skill) == installed)
        run('doctor', [skill / 'scripts/doctor.py'])
        exact = run('host_time_exact', [skill / 'scripts/evidence_time.py', 'interval', '--start', '2026-11-11T16:25:49.2035066Z', '--end', '2026-11-11T16:28:49.2201725+00:00', '--minimum', '180'])
        check('host_time_precise', json.loads(exact.stdout)['elapsed_seconds']=='180.016665900' and json.loads(exact.stdout)['passed'] is True)
        below = run('host_time_below_boundary', [skill / 'scripts/evidence_time.py', 'interval', '--start', '2026-11-11T16:25:49Z', '--end', '2026-11-11T16:28:48.9999999Z', '--minimum', '180'], 1)
        check('host_time_never_rounds_to_pass', json.loads(below.stdout)['passed'] is False)

        smoke = run('smoke', [skill / 'scripts/doctor.py', '--smoke', output / '独立项目 smoke'])
        check('smoke_accepts_task_and_audits_clean', json.loads(smoke.stdout)['synthetic_smoke_passed'])
        snapshot = tree(output / '独立项目 smoke')
        run('smoke_existing_refused', [skill / 'scripts/doctor.py', '--smoke', output / '独立项目 smoke'], 2)
        check('existing_project_unchanged', tree(output / '独立项目 smoke') == snapshot)
        check('no_installed_mutation_from_doctor', tree(skill) == installed)
        (skill / 'SKILL.md').write_text('user local modifications', encoding='utf-8')
        changed = tree(skill)
        run('conflicting_install_refused', [installer, 'install', '--skills-dir', destination], 2)
        check('user_modifications_preserved', tree(skill) == changed)
        check('release_unchanged_after_checks', tree(bundle) == before)
        bad = output / 'tampered'
        shutil.copytree(str(bundle), str(bad))
        (bad / 'adaptive-project/scripts/runtime.py').write_text('corrupt', encoding='utf-8')
        run('corruption_rejected', [bad / 'install.py', 'verify'], 2)
        denied_target = output / 'must-not-be-created'
        run('corruption_install_refused', [bad / 'install.py', 'install', '--skills-dir', denied_target], 2)
        check('tampered_install_has_no_destination_side_effects', not denied_target.exists())
        shutil.copyfile(bundle / 'adaptive-project/scripts/runtime.py', bad / 'adaptive-project/scripts/runtime.py')
        (bad / 'unlisted.txt').write_text('unexpected', encoding='utf-8')
        run('unexpected_file_rejected', [bad / 'install.py', 'verify'], 2)
        (bad / 'unlisted.txt').unlink()
        sums = json.loads((bad / 'SHA256SUMS.json').read_text(encoding='utf-8'))
        sums['files']['../outside'] = '0' * 64
        (bad / 'SHA256SUMS.json').write_text(json.dumps(sums), encoding='utf-8')
        run('manifest_path_escape_rejected', [bad / 'install.py', 'verify'], 2)
        result = {'passed': True, 'checks': checks, 'platform': sys.platform,
                  'scope': 'Local relocated installation and synthetic runtime checks; no other device or host discovery tested'}
    except Exception as error:
        result = {'passed': False, 'checks': checks, 'error': str(error)}
    (output / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

