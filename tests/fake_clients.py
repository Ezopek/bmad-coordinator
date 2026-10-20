"""Fake worker clients, two test-only adapters and a shared harness. They prove coordinator logic for the protocol as
the fakes encode it; nothing here is evidence about a live client (design section 10, "Test clients")."""

import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

from coordinator_core import clients, worker
from coordinator_core.model import DomainError
from coordinator_core.run_receipt import canonical, run_name, run_root
from tests.bmad_fixture import make_bmad, write
from tests.test_bmad_core import CoreBase
from tests.test_bmad_review import ReviewCase
from tests.test_bmad_planning import SCOPE

EXECUTABLES = {"codex-cli": "codex", "alt-stdout": "alt-stdout", "alt-file": "alt-file", "claude-code": "claude",
               "copilot": "copilot"}
FORBIDDEN = {
    "codex-cli": ("resume", "--last", "fork", "--dangerously-bypass-approvals-and-sandbox", "--yolo",
                  "--full-auto"),
    "alt-stdout": ("--resume", "--continue", "--fork", "--bypass"),
    "alt-file": ("--resume", "--continue", "--fork", "--bypass"),
    "claude-code": ("--resume", "-r", "--continue", "-c", "--fork-session", "--dangerously-skip-permissions",
                    "--allow-dangerously-skip-permissions", "bypassPermissions"),
    "copilot": ("--allow-all-tools", "--allow-all-paths", "--yolo", "--resume", "--continue", "--enable-memory",
                "--additional-mcp-config", "--agent"),
}

# The fake `claude` (design section 10): the section 5 argv contract and the section 6 stream as the docs describe
# it. Knobs: FAKE_SESSION (echoed session), FAKE_SUBTYPE (result subtype), FAKE_DENIALS (permission_denials),
# FAKE_TOOLS (init tools; unset: the requested --tools; null: no tools field; []: an empty list),
# FAKE_MCP (init mcp_servers; null: absent), FAKE_READ (paths) and FAKE_BASH ([command, is_error] pairs) as
# tool_use/tool_result pairs. Modes: truncated, no-init, no-structured, is-error, auth-fail, bad-schema, sleep, exit-1,
# max-turns (design D25: an `error_max_turns` result without structured output, exit 1).
FAKE_CLAUDE = r'''
def claude_argv(args):
    def flag(name):
        assert args.count(name) == 1, name
        return args[args.index(name) + 1]
    assert args[0] == '-p' and flag('--output-format') == 'stream-json'
    assert '--verbose' in args and '--no-session-persistence' in args and '--strict-mcp-config' in args
    session = flag('--session-id')
    assert uuid.UUID(session).version == 4 and str(uuid.UUID(session)) == session
    assert flag('--model') and int(flag('--max-turns')) > 0 and isinstance(json.loads(flag('--json-schema')), dict)
    assert flag('--setting-sources') == '' and flag('--mcp-config') == '{"mcpServers":{}}'
    start = args.index('--disallowedTools') + 1
    assert args.count('--disallowedTools') == 1  # one occurrence: mcp__* then, for authors, the deny rules
    end = next(index for index in range(start, len(args)) if args[index].startswith('--'))
    denied = args[start:end]
    permission, tools = flag('--permission-mode'), flag('--tools')
    if permission == 'acceptEdits':
        assert tools in ('Read,Grep,Glob,Edit,Write,Bash', 'Read,Grep,Glob,Edit,Write,Bash,WebFetch,WebSearch')
        assert flag('--permission-prompts') == 'none'
        assert '--add-dir' not in args
        assert denied == ['mcp__*'] + [tool + '(' + path + ')' for tool in ('Edit', 'Write', 'MultiEdit',
                                       'NotebookEdit') for path in ('_bmad/**', '.agents/**', '.claude/**',
                                       '.bmad-coordinator/**', 'AGENTS.md', 'CLAUDE.md')]
        rules = args[args.index('--allowedTools') + 1:] if '--allowedTools' in args else []
        if tools.endswith(',WebFetch,WebSearch'):
            assert rules[-2:] == ['WebFetch', 'WebSearch']
            rules = rules[:-2]
        assert not rules or (rules[-1] == 'Bash(sha256sum)' and
                             all(rule.startswith('Bash(') and rule.endswith(' *)') for rule in rules[:-1]))
    else:
        assert permission == 'dontAsk' and '--permission-prompts' not in args and denied == ['mcp__*']
        assert (tools, flag('--allowedTools')) in (('Read,Grep,Glob,Bash', 'Read,Grep,Glob,Bash(sha256sum *)'),
                                                   ('Read,Grep,Glob', 'Read,Grep,Glob'))
        dirs = [args[index + 1] for index, item in enumerate(args) if item == '--add-dir']
        assert all(os.path.isabs(item) and os.path.isdir(item) for item in dirs)
        assert not dirs or tools == 'Read,Grep,Glob,Bash'
    return session, permission, tools.split(',')


def claude_emit(emit, preset, permission, requested, mode, payload, cwd):
    session = os.environ.get('FAKE_SESSION') or preset
    init = {'type': 'system', 'subtype': 'init', 'session_id': session, 'cwd': str(cwd), 'model': 'claude-fake-1',
            'permissionMode': permission}
    tools = json.loads(os.environ['FAKE_TOOLS']) if 'FAKE_TOOLS' in os.environ else requested
    if tools is not None:
        init['tools'] = tools
    servers = json.loads(os.environ.get('FAKE_MCP', '[]'))
    if servers is not None:
        init['mcp_servers'] = servers
    if mode != 'no-init':
        emit(init)
    if mode == 'auth-fail':
        emit({'type': 'result', 'subtype': 'success', 'is_error': True, 'session_id': session,
              'result': 'Not logged in. Please run /login'})
        return
    if mode == 'max-turns':   # design D25: the turn-limit stop as observed live: no structured output, exit 1
        emit({'type': 'result', 'subtype': 'error_max_turns', 'is_error': True, 'session_id': session,
              'terminal_reason': 'max_turns', 'stop_reason': 'tool_use', 'num_turns': 61,
              'errors': ['Reached maximum number of turns (60)'], 'total_cost_usd': 0.0123, 'permission_denials': [],
              'modelUsage': {'claude-fake-1': {'inputTokens': 1, 'outputTokens': 1, 'costUSD': 0.0123}},
              'usage': {'input_tokens': 1, 'output_tokens': 1}})
        return
    calls = [('Read', {'file_path': path}, False) for path in json.loads(os.environ.get('FAKE_READ', '[]'))]
    calls += [('Bash', {'command': command}, failed) for command, failed in json.loads(os.environ.get('FAKE_BASH', '[]'))]
    for index, (name, given, failed) in enumerate(calls):
        tool = 'toolu_%d' % index
        emit({'type': 'assistant', 'session_id': session, 'message': {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': tool, 'name': name, 'input': given}]}})
        emit({'type': 'user', 'session_id': session, 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': tool, 'content': 'denied' if failed else 'ok', 'is_error': failed}]}})
    if mode == 'truncated':
        return
    result = {'type': 'result', 'subtype': os.environ.get('FAKE_SUBTYPE', 'success'), 'is_error': mode == 'is-error',
              'session_id': session, 'result': 'done', 'num_turns': 3, 'stop_reason': 'end_turn',
              'terminal_reason': 'completed', 'total_cost_usd': 0.0123, 'duration_ms': 1500, 'duration_api_ms': 1200,
              'permission_denials': json.loads(os.environ.get('FAKE_DENIALS', '[]')),
              'modelUsage': {'claude-fake-1': {'inputTokens': 1, 'outputTokens': 1, 'costUSD': 0.0123}},
              'usage': {'input_tokens': 1, 'output_tokens': 1}}
    if mode != 'no-structured':
        result['structured_output'] = payload
    emit(result)
'''

# The fake `copilot` (design section 14): the author-only argv grammar with its write deny rules, the pinned
# --available-tools, a bound author's launcher rule `shell(bmad-script)` (never `uv`) with the launcher present (0500
# in a 0700 `worker-tools/`, that directory first on PATH; the envelope names no absolute path), an empty per-run
# COPILOT_HOME without COPILOT_ALLOW_ALL, the envelope from `-p`, FAKE_LAUNCH ([argv, ...] run as given, `@launcher`
# becoming the absolute launcher path and a bare `bmad-script` resolved through PATH; exit codes and stderr to
# FAKE_LAUNCH_OUT), a return file written per FAKE_RETURN
# (ok|missing|symlink|hardlink|oversize|deep-nesting|invalid|generic-invalid|extra-file|github-dir), the session folder
# `session-state/<id>/workspace.yaml` (cwd, created_at), FAKE_LOCKS (own: the live `.session-operation-locks/<id>.lock`;
# extra|foreign|empty|file|other-dot: malformed variants), a log file, then the JSONL stream: session.start, FAKE_TOOLS
# ([name, path] pairs as tool.execution_start with toolName and a nameless tool.execution_complete, as the bundled
# 1.0.91 event schema shapes them), FAKE_TOOL_EVENTS (raw [type, data] records; "@return" becomes the return path), a
# decoy JSON assistant.message, and a last `result` with sessionId, exitCode and usage. Modes: truncated, no-result,
# wrong-session, exit-code-1, exit-code-only (result exitCode 1, process exit 0), session-error, sleep,
# preexisting-session (a second session folder), cwd-mismatch, early-created, home-leak
# ($HOME/.copilot/session-state/<id>), plus the shared payload modes.
FAKE_COPILOT = r'''
import subprocess
from datetime import datetime, timedelta, timezone
PINNED_TOOLS = __PINNED_TOOLS__


def copilot_argv(args):
    assert not any(item.split('=', 1)[0] in FORBIDDEN for item in args), 'forbidden flag'
    valued = {'-p', '--output-format', '--session-id', '--model', '--log-dir', '-C', '--add-dir', '--available-tools',
              '--allow-tool', '--deny-tool'}
    switches = {'--no-ask-user', '--no-auto-update', '--no-custom-instructions', '--disable-builtin-mcps',
                '--disallow-temp-dir'}
    index = 0
    while index < len(args):  # the exact grammar: every token is a known switch or a known flag with its value
        assert args[index] in valued | switches, args[index]
        index += 2 if args[index] in valued else 1
    def flag(name):
        assert args.count(name) == 1, name
        return args[args.index(name) + 1]
    assert all(args.count(name) == 1 for name in switches)
    assert flag('--output-format') == 'json' and flag('--model') and flag('--available-tools') == PINNED_TOOLS
    session = flag('--session-id')
    assert uuid.UUID(session).version == 4 and str(uuid.UUID(session)) == session
    cwd = pathlib.Path(flag('-C'))
    assert os.path.realpath(cwd) == os.path.realpath(os.getcwd())
    home = pathlib.Path(os.environ['COPILOT_HOME'])
    assert home.is_dir() and os.listdir(home) == [] and 'COPILOT_ALLOW_ALL' not in os.environ
    assert os.environ.get('COPILOT_AUTO_UPDATE') == 'false'
    logs, added = pathlib.Path(flag('--log-dir')), pathlib.Path(flag('--add-dir'))
    assert logs == home.parent / 'copilot-logs' and os.listdir(logs) == []
    assert added == home.parent / 'worker-return' and os.listdir(added) == []
    tools = [args[index + 1] for index, item in enumerate(args) if item == '--allow-tool']
    rules = tools[1:]
    assert tools[0] == 'write' and rules in ([], ['shell(bmad-script)', 'shell(sha256sum)']), tools
    denied = [args[index + 1] for index, item in enumerate(args) if item == '--deny-tool']
    assert denied == ['write(%s)' % path for path in ('_bmad/**', '.agents/**', '.claude/**', '.bmad-coordinator/**',
                                                      'AGENTS.md', 'CLAUDE.md', '.github/**')], denied
    assert 'script_launcher' not in json.loads(flag('-p'))  # bare name only: no absolute-path fallback is offered
    launcher = str(home.parent / 'worker-tools' / 'bmad-script') if rules else None
    if rules:  # a bound author: the coordinator-written launcher exists before the spawn
        path = pathlib.Path(launcher)
        assert path.is_file() and not path.is_symlink()
        assert stat.S_IMODE(path.stat().st_mode) == 0o500 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert os.environ['PATH'].split(os.pathsep)[0] == str(path.parent)
    else:
        assert not os.path.lexists(home.parent / 'worker-tools')
        assert str(home.parent / 'worker-tools') not in os.environ['PATH'].split(os.pathsep)
    return session, cwd, home, added / 'return.json', flag('-p'), launcher


def copilot_return(target, home, payload):
    kind = os.environ.get('FAKE_RETURN', 'ok')
    if kind == 'generic-invalid':
        payload = dict(payload, changed_paths='not-a-list')
    if kind == 'deep-nesting':
        nested = []
        for _ in range(100):
            nested = [nested]
        payload = dict(payload, checks=nested)
    data = 'not json {' if kind == 'invalid' else json.dumps(payload)
    if kind == 'oversize':
        data += ' ' * (1 << 20)
    if kind == 'missing':
        return
    if kind == 'symlink':
        (home / 'return-target.json').write_text(data)
        target.symlink_to(home / 'return-target.json')
        return
    target.write_text(data)
    if kind == 'hardlink':
        os.link(target, home / 'return-link.json')
    if kind == 'extra-file':
        (target.parent / 'extra.json').write_text('{}')
    if kind == 'github-dir':
        (target.parent / '.github' / 'agents').mkdir(parents=True)
        (target.parent / '.github' / 'agents' / 'trusted.md').write_text('loaded as trusted\n')


def copilot_locks(state, session):
    kind, locks = os.environ.get('FAKE_LOCKS'), state / '.session-operation-locks'
    if kind == 'file':
        locks.write_text('')
    elif kind:
        locks.mkdir()
        names = {'own': [session], 'other-dot': [session], 'extra': [session, str(uuid.uuid4())],
                 'foreign': [str(uuid.uuid4())], 'empty': []}[kind]
        for name in names:
            (locks / (name + '.lock')).write_text('')
        if kind == 'other-dot':
            (state / '.other').mkdir()


def copilot_launch(launcher):
    results = []
    for argv in json.loads(os.environ.get('FAKE_LAUNCH', '[]')):
        done = subprocess.run([launcher if item == '@launcher' else item for item in argv], capture_output=True,
                              text=True)
        results.append([done.returncode, done.stderr])
    if os.environ.get('FAKE_LAUNCH_OUT'):
        pathlib.Path(os.environ['FAKE_LAUNCH_OUT']).write_text(json.dumps(results))


def copilot_emit(emit, session, mode, payload, cwd, home, target, launcher):
    state = home / 'session-state'
    (state / session).mkdir(parents=True)
    copilot_locks(state, session)
    copilot_launch(launcher)
    created = datetime.now(timezone.utc) - timedelta(hours=1 if mode == 'early-created' else 0)
    (state / session / 'workspace.yaml').write_text('id: %s\ncwd: %s\ncreated_at: %s\n' % (
        session, '/elsewhere' if mode == 'cwd-mismatch' else cwd,
        created.isoformat(timespec='milliseconds').replace('+00:00', 'Z')))
    (home.parent / 'copilot-logs' / 'process.log').write_text('fake log\n')
    (state / session / 'events.jsonl').write_text(json.dumps({'type': 'session.start', 'data': {}}) + '\n' + json.dumps(
        {'type': 'session.shutdown', 'data': {'usage': {'inputTokens': 2000, 'outputTokens': 80, 'cacheReadTokens': 900,
                                                        'cacheWriteTokens': 100, 'reasoningTokens': 10},
                                              'requests': {'claude-fake-1': 1}}}) + '\n')
    if mode == 'preexisting-session':
        (state / str(uuid.uuid4())).mkdir()
    if mode == 'home-leak':
        (pathlib.Path(os.environ['HOME']) / '.copilot' / 'session-state' / session).mkdir(parents=True)
    copilot_return(target, home, payload)
    emit({'type': 'session.start', 'data': {'sessionId': session, 'copilotVersion': '9.9.9', 'cwd': str(cwd)}})
    for index, (name, path) in enumerate(json.loads(os.environ.get('FAKE_TOOLS', '[]'))):
        call = 'call_%d' % index
        emit({'type': 'tool.execution_start', 'data': {'toolCallId': call, 'toolName': name, 'arguments': {'path': path}}})
        emit({'type': 'tool.execution_complete', 'data': {'toolCallId': call, 'success': True}})
    for kind, data in json.loads(os.environ.get('FAKE_TOOL_EVENTS', '[]').replace('@return', str(target))):
        emit({'type': kind, 'data': data})
    decoy = dict(payload, result='blocked', checks=['decoy final answer'])
    emit({'type': 'assistant.message', 'data': {'content': json.dumps(decoy), 'phase': 'final_answer'}})
    if mode == 'session-error':
        emit({'type': 'session.error', 'data': {'message': 'fake session error'}})
    if mode == 'truncated':
        sys.stdout.write('{"type": "res')
        sys.stdout.flush()
        return
    if mode == 'no-result':
        return
    emit({'type': 'result', 'sessionId': str(uuid.uuid4()) if mode == 'wrong-session' else session,
          'exitCode': 1 if mode in ('exit-code-1', 'exit-code-only') else 0,
          'usage': {'premiumRequests': 1, 'totalApiDurationMs': 12, 'sessionDurationMs': 34}})
'''

FAKE = r'''#!/usr/bin/env python3
import hashlib, json, os, pathlib, stat, sys, time, uuid
PROTOCOL = __PROTOCOL__
FORBIDDEN = __FORBIDDEN__
__CLAUDE__
__COPILOT__
args = sys.argv[1:]
if args == ['--version']:
    if os.environ.get('FAKE_VERSION_COUNT'):
        with open(os.environ['FAKE_VERSION_COUNT'], 'a') as out:
            out.write('version\n')
    if os.environ.get('FAKE_VERSION_ENV_OUT'):
        pathlib.Path(os.environ['FAKE_VERSION_ENV_OUT']).write_text(json.dumps(sorted(os.environ)))
    failure = os.environ.get('FAKE_VERSION_FAIL')
    if failure == 'empty':
        sys.exit(0)
    print({'claude-code': '9.9.9 (Claude Code fake)',
           'copilot': 'GitHub Copilot CLI 9.9.9.\nRun copilot update to check for updates (fake hint).'}
          .get(PROTOCOL, PROTOCOL + ' fake 1'))
    sys.exit(1 if failure == 'exit' else 0)
if any(item in FORBIDDEN for item in args):
    print('forbidden flag', file=sys.stderr)
    sys.exit(3)
def opt(name):
    return args[args.index(name) + 1]
if PROTOCOL == 'codex-cli':
    assert args[0:2] == ['exec', '-']
    cwd = pathlib.Path(opt('-C'))
    assert pathlib.Path(opt('--output-schema')).is_file()
    last = pathlib.Path(opt('--output-last-message'))
    raw = sys.stdin.buffer.read()
    session = os.environ.get('FAKE_THREAD') or uuid.uuid4().hex
elif PROTOCOL == 'claude-code':
    session, permission, requested = claude_argv(args)
    cwd, raw = pathlib.Path(os.getcwd()), sys.stdin.buffer.read()
elif PROTOCOL == 'copilot':
    session, cwd, home, target, prompt, launcher = copilot_argv(args)
    raw = prompt.encode()
elif PROTOCOL == 'alt-stdout':
    assert args[0] == 'run' and opt('--model')
    cwd, session, raw = pathlib.Path(opt('--cwd')), opt('--session'), opt('--prompt').encode()
else:
    assert args[0] == 'go'
    cwd, session, target = pathlib.Path(opt('--cwd')), opt('--session'), pathlib.Path(opt('--return'))
    raw = sys.stdin.buffer.read()
envelope = json.loads(raw)
assert envelope['assignment_id'] and envelope['request_fingerprint']
if PROTOCOL in ('alt-file', 'copilot'):
    assert envelope['return_path'] == str(target)
with open(os.environ['FAKE_COUNT'], 'a') as out:
    out.write('spawn\n')
if os.environ.get('FAKE_ENVELOPE_OUT'):
    pathlib.Path(os.environ['FAKE_ENVELOPE_OUT']).write_text(hashlib.sha256(raw).hexdigest())
def listing(path):
    return {'entries': sorted(os.listdir(path)), 'mode': stat.S_IMODE(os.stat(path).st_mode)}
if os.environ.get('FAKE_ENV_OUT'):
    info = {'env': {key: os.environ.get(key) for key in ('BMAD_TEST', 'ALT_HOME', 'ALT_ALLOW_ALL', 'ALT_STRICT',
                                                         'PYTHONDONTWRITEBYTECODE', 'DISABLE_AUTOUPDATER',
                                                         'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS',
                                                         'CLAUDE_CODE_DISABLE_AUTO_MEMORY', 'COPILOT_HOME',
                                                         'COPILOT_AUTO_UPDATE', 'COPILOT_ALLOW_ALL')},
            'names': sorted(os.environ)}  # names only, never values
    if os.environ.get('ALT_HOME'):
        info['alt_home'] = listing(os.environ['ALT_HOME'])
    if PROTOCOL in ('alt-file', 'copilot'):
        info['worker_return'] = listing(target.parent)
    if PROTOCOL == 'copilot':
        info['copilot_home'] = listing(home)
    pathlib.Path(os.environ['FAKE_ENV_OUT']).write_text(json.dumps(info))
if os.environ.get('FAKE_WAIT_FOR'):
    deadline = time.time() + 10
    while not os.path.exists(os.environ['FAKE_WAIT_FOR']) and time.time() < deadline:
        time.sleep(.02)
mode = os.environ.get('FAKE_MODE', 'completed')
for path in json.loads(os.environ.get('FAKE_OUTSIDE_WRITE', '[]')):
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(path).write_text('outside\n')
for action, path in json.loads(os.environ.get('FAKE_TAMPER', '[]')):
    if action == 'modify':
        os.chmod(path, 0o600)
        with open(path, 'ab') as out:
            out.write(b' ')
    else:
        os.chmod(os.path.dirname(path), 0o700)
        os.unlink(path)
if os.environ.get('FAKE_RUN_FILE'):
    os.close(os.open(os.environ['FAKE_RUN_FILE'], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
if mode == 'sleep':
    time.sleep(30)
    sys.exit(0)
claimed = []
for path, text in json.loads(os.environ.get('FAKE_WRITE', '[]')):
    (cwd / path).parent.mkdir(parents=True, exist_ok=True)
    (cwd / path).write_text(text)
    claimed.append(path)
for path, link in json.loads(os.environ.get('FAKE_LINK', '[]')):
    (cwd / path).parent.mkdir(parents=True, exist_ok=True)
    (cwd / path).symlink_to(link)
    claimed.append(path)
for path in json.loads(os.environ.get('FAKE_DELETE', '[]')):
    (cwd / path).unlink()
    claimed.append(path)
payload = dict(assignment_id=envelope['assignment_id'], request_fingerprint=envelope['request_fingerprint'],
               result='completed', changed_paths=claimed, checks=[], descendants=[], unresolved_question=None,
               options=[], affected_ids=[])
payload.update(json.loads(os.environ.get('FAKE_EXTRA', '{}')))
if mode == 'blocked':
    payload['result'] = 'blocked'
if mode == 'needs-decision':
    payload['result'] = 'needs-decision'
    payload['unresolved_question'] = 'Which option?'
if mode == 'wrong-id':
    payload['assignment_id'] = 'wrong'
if mode == 'bad-schema':
    payload['unexpected'] = True
tools = json.loads(os.environ.get('FAKE_TOOLS', '[["Read", "brief.md"]]'))
def emit(value):
    print(json.dumps(value), flush=True)
if PROTOCOL == 'codex-cli':
    last.write_text(json.dumps(payload))
    emit({'type': 'thread.started', 'thread_id': session})
    if mode != 'truncated':
        emit({'type': 'turn.completed', 'usage': {'input_tokens': 1000, 'cached_input_tokens': 400,
                                                  'cache_write_input_tokens': 0, 'output_tokens': 50,
                                                  'reasoning_output_tokens': 5}})
elif PROTOCOL == 'claude-code':
    claude_emit(emit, session, permission, requested, mode, payload, cwd)
elif PROTOCOL == 'copilot':
    copilot_emit(emit, session, mode, payload, cwd, home, target, launcher)
else:
    if PROTOCOL == 'alt-file':
        (pathlib.Path(os.environ['ALT_HOME']) / (session + '.session')).write_text('fresh\n')
        target.write_text(json.dumps(payload))
    emit({'type': 'start', 'session': session})
    for name, path in tools:
        emit({'type': 'tool', 'session': session, 'name': name, 'path': path})
    if mode != 'truncated':
        end = {'type': 'end', 'session': session, 'ok': True}
        if PROTOCOL == 'alt-stdout':
            end['final'] = payload
        emit(end)
linger = os.environ.get('FAKE_LINGER')
if linger:
    sys.stdout.flush()
    if os.fork() == 0:
        try:
            time.sleep(.5)
            (cwd / 'linger.md').write_text('late\n')
            watch, marker = os.environ.get('FAKE_LINGER_WATCH'), os.environ.get('FAKE_LINGER_MARKER')
            deadline = time.time() + (60 if linger == 'hang' else 0)
            while time.time() < deadline:
                if watch and marker and os.path.exists(watch):
                    with open(marker, 'a') as out:
                        out.write('end-seen\n')
                time.sleep(.02)
        finally:
            os._exit(0)
sys.exit(1 if mode in ('exit-1', 'auth-fail', 'exit-code-1', 'max-turns') else 0)
'''


def install(bin_dir: Path, client: str) -> Path:
    path = Path(bin_dir) / EXECUTABLES[client]
    path.write_text(FAKE.replace("__PROTOCOL__", repr(client)).replace("__FORBIDDEN__", repr(FORBIDDEN[client]))
                    .replace("__CLAUDE__", FAKE_CLAUDE).replace("__COPILOT__", FAKE_COPILOT)
                    .replace("__PINNED_TOOLS__", repr(",".join(sorted(clients.COPILOT_TOOLS)))))
    path.chmod(0o755)
    return path


def safe_path(bin_dir: Path) -> str:
    """An empty bin directory plus only the directories of git and the test interpreter: no client is on it."""
    dirs = [str(bin_dir), os.path.dirname(shutil.which("git")), os.path.dirname(sys.executable)]
    return os.pathsep.join(dict.fromkeys(dirs))


# Two test-only adapters (design section 10). Production never registers them.
ALT_REQUESTED = {"read-only": frozenset({"Read"}), "workspace-write": frozenset({"Read", "Write"})}
ALT_INTERNAL = frozenset({"Note"})


def _alt_records(run_dir: Path, launch: dict) -> tuple[list[dict], dict]:
    try:
        seen = clients.jsonl_records(run_dir / "events.jsonl")
        session = launch.get("session_id")
        kinds = [item["type"] for item in seen]
        if not isinstance(session, str) or not seen or kinds[0] != "start" or kinds[-1] != "end" or \
           kinds.count("start") != 1 or kinds.count("end") != 1 or \
           any(item.get("session") != session for item in seen) or seen[-1].get("ok") is not True:
            raise ValueError("alt stream lacks one start, one end or the preset session")
        tools = [item.get("name") for item in seen if item["type"] == "tool"]
        if any(type(name) is not str for name in tools):
            raise ValueError("tool record without a name")
        return seen, ({"tools": tools} if tools else {})
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise DomainError("UNKNOWN_EVIDENCE", "Alt terminal record is missing or malformed") from exc


def _alt_stdout_terminal(run_dir: Path, launch: dict) -> clients.Terminal:
    seen, facts = _alt_records(run_dir, launch)
    final = seen[-1].get("final")
    if not isinstance(final, dict):
        raise DomainError("UNKNOWN_EVIDENCE", "Alt end record has no final object")
    return clients.Terminal(launch["session_id"], canonical(final), facts)


def _alt_file_observe(run_dir: Path, launch: dict) -> dict:
    return {"sessions": sorted(os.listdir(run_dir / "alt-home"))}


def _alt_file_terminal(run_dir: Path, launch: dict) -> clients.Terminal:
    seen, facts = _alt_records(run_dir, launch)
    try:
        fresh = json.loads((run_dir / "freshness.json").read_bytes())
    except (OSError, ValueError) as exc:
        raise DomainError("UNKNOWN_EVIDENCE", "Sealed freshness observation is missing") from exc
    if "final" in seen[-1] or fresh != {"sessions": [launch["session_id"] + ".session"]}:
        raise DomainError("UNKNOWN_EVIDENCE", "Alt-file session is not fresh or the stream carries a final")
    return clients.Terminal(launch["session_id"], clients.read_worker_return(run_dir), facts)


ALT_STDOUT = clients.Client(
    name="alt-stdout", executable="alt-stdout", confinement="tool-policy", presets_session=True,
    return_source="stdout", envelope_via="argv", max_envelope_bytes=None, write_free_readonly=True,
    max_turns_default=7, env={}, env_unset=frozenset(), run_subdirs=(), private_subdirs=(), observation_name=None,
    internal_tools=ALT_INTERNAL, run_env=lambda spec: {},
    allowed_tools=lambda mode: ALT_REQUESTED[mode] | ALT_INTERNAL,
    argv=lambda spec: [spec.binary, "run", "--cwd", str(spec.worktree), "--model", spec.model,
                       "--session", spec.session_id, "--prompt", clients.ENVELOPE_ARG],
    launch_facts=lambda spec: {"alt_protocol": 1}, observe=None, terminal=_alt_stdout_terminal)
ALT_FILE = clients.Client(
    name="alt-file", executable="alt-file", confinement="tool-policy", presets_session=True,
    return_source="worker-file", envelope_via="stdin", max_envelope_bytes=16384, write_free_readonly=False,
    max_turns_default=None, env={"ALT_STRICT": "1"}, env_unset=frozenset({"ALT_ALLOW_ALL"}),
    run_subdirs=(clients.WORKER_RETURN, "alt-home"), private_subdirs=("alt-home",),
    observation_name="freshness.json", internal_tools=frozenset(),
    run_env=lambda spec: {"ALT_HOME": str(spec.run_dir / "alt-home")}, allowed_tools=None,
    argv=lambda spec: [spec.binary, "go", "--cwd", str(spec.worktree), "--session", spec.session_id,
                       "--return", spec.return_path],
    launch_facts=lambda spec: {}, observe=_alt_file_observe, terminal=_alt_file_terminal)
TEST_CLIENTS = {ALT_STDOUT.name: ALT_STDOUT, ALT_FILE.name: ALT_FILE}

# Golden normalizer, verbatim from tests/fixtures/codex-322752a/README.txt.
UUID = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TIME = re.compile(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00")


def golden_normalize(data: bytes, literals: dict[str, str], pids=()) -> bytes:
    for literal, placeholder in sorted(literals.items(), key=lambda item: -len(item[0])):
        data = data.replace(literal.encode(), placeholder.encode())
    data = UUID.sub(lambda m: b"<UUID:%d>" % uuid.UUID(m.group().decode()).int, data)
    data = TIME.sub(b"<TIME>", data)
    for pid in pids:
        data = data.replace(b'"%d"' % pid, b'"<PID>"')
        data = re.sub(rb'(?<=:)%d(?=[,}])' % pid, b'"<PID>"', data)
    return data


class Harness(ReviewCase):
    """CoreBase repository and owner, the three fakes on PATH and the two test adapters registered. The BMAD
    fixture (PlanningCase/ReviewCase helpers) is added only by tests that call planning()."""

    CLIENT = clients.DEFAULT

    def setUp(self):
        CoreBase.setUp(self)
        for name in EXECUTABLES:
            install(self.fake_dir, name)
        registry = patch.dict(clients.REGISTRY, TEST_CLIENTS)
        registry.start()
        self.addCleanup(registry.stop)
        self.versions = self.root / "versions.log"
        self.env["FAKE_VERSION_COUNT"] = str(self.versions)

    def planning(self):
        uv = self.fake_dir / "uv"
        uv.write_text("#!/bin/sh\necho uv-fake 1\n")
        uv.chmod(0o755)
        environ = patch.dict(os.environ, self.env)
        environ.start()
        self.addCleanup(environ.stop)
        make_bmad(self.t.path)
        write(self.t.path, "docs/input.md", "Driving input.\n")
        self.fid = self.add_feature(self.t, "feature", [SCOPE])
        self.compat = self.judge()

    @property
    def client(self) -> clients.Client:
        return clients.get(self.CLIENT)

    def version_count(self) -> int:
        return len(self.versions.read_text().splitlines()) if self.versions.exists() else 0

    def assert_fakes(self):
        """Every launch finds the fakes, never a real client."""
        for executable in EXECUTABLES.values():
            self.assertEqual(self.fake_dir, Path(shutil.which(executable)).parent)

    def go(self, assignment, t=None, timeout=5, client=None, max_turns=None, **env):
        """One attempt with the client under test (or `client`)."""
        t = t or self.t
        with patch.dict(os.environ, {**self.env, **env}):
            self.assert_fakes()
            return worker.run_assignment(t.repo, self.store, self.owner, assignment, "fake-model", timeout,
                                         client or self.CLIENT, max_turns)

    def run_it(self, assignment, t=None, timeout=5, **env):
        """CoreBase.run_it (the binding or default client decides) with the fake assertion."""
        with patch.dict(os.environ, {**self.env, **env}):
            self.assert_fakes()
        return super().run_it(assignment, t, timeout, **env)

    def run_dir(self, assignment, t=None) -> Path:
        row = self.coordinator.detail("assignment", assignment)
        return run_root((t or self.t).repo) / run_name(assignment, row["request_fingerprint"])

    def receipt(self, report, name="return.json") -> dict:
        return json.loads((Path(report.run_dir) / name).read_bytes())

    def reserved(self, assignment) -> list:
        return [row["source_id"] for row in self.events("assignment.launch-reserved", assignment)]
