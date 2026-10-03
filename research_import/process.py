"""Run one external tool with fixed arguments, no shell, hard limits and a full tree kill.

A timeout, too much output or a download growing past its cap kills the whole process
tree (taskkill /T on Windows, the process group elsewhere), waits for the kill, and marks
the result `terminated`; a terminated result is always a failure, whatever its exit code.
The child gets an allowlisted environment; the provider key reaches only the reader.
"""
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

MAX_OUTPUT_BYTES = 1024 * 1024
PROVIDER_KEY_NAMES = ('GEMINI_API_KEY', 'GOOGLE_API_KEY')
SAFE_ENV_NAMES = ('PATH', 'Path', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'HOME', 'APPDATA', 'LOCALAPPDATA')


def child_environment(env, include_provider_key=False):
    """An allowlisted environment for a child process. Keys are included only on request."""
    safe = {name: env[name] for name in SAFE_ENV_NAMES if name in env}
    if include_provider_key:
        safe.update({name: env[name] for name in PROVIDER_KEY_NAMES if env.get(name)})
    safe.update(PYTHONNOUSERSITE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    return safe


def kill_tree(proc):
    try:
        if os.name == 'nt':
            taskkill = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'taskkill.exe'
            subprocess.run([str(taskkill), '/PID', str(proc.pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_bounded(args, *, env, cwd=None, input_text='', timeout=600, max_output=MAX_OUTPUT_BYTES, max_file_bytes=None):
    """Run `args` (a list, never a string) and return {code, stdout, stderr, terminated}.

    `terminated` is None, 'timeout', 'output_limit' or 'file_limit'. A missing executable
    raises FileNotFoundError, which the caller reports as an unavailable tool.
    """
    options = {'stdin': subprocess.PIPE, 'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE, 'env': env, 'cwd': cwd,
               'shell': False}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    else:
        options['start_new_session'] = True
    proc = subprocess.Popen(list(args), **options)
    state = {'terminated': None, 'bytes': 0}
    buffers = {'stdout': [], 'stderr': []}
    lock = threading.Lock()

    def stop(reason):
        with lock:
            if state['terminated']:
                return
            state['terminated'] = reason
        kill_tree(proc)

    def pump(name, pipe):
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    return
                with lock:
                    state['bytes'] += len(chunk)
                    over = state['bytes'] > max_output
                    if not over and not state['terminated']:
                        buffers[name].append(chunk)
                if over:
                    stop('output_limit')
                    return
        except (OSError, ValueError):
            return

    def feed():
        try:
            proc.stdin.write(input_text.encode('utf-8'))
        except OSError:
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def watch():
        while proc.poll() is None and not state['terminated']:
            time.sleep(0.2)
            try:
                with os.scandir(cwd) as entries:
                    for entry in entries:
                        # os.stat, not entry.stat(): on Windows a directory entry's size lags behind a file still being written.
                        if entry.is_file() and os.stat(entry.path).st_size > max_file_bytes:
                            stop('file_limit')
                            return
            except OSError:
                pass

    threads = [threading.Thread(target=pump, args=(n, p), daemon=True) for n, p in (('stdout', proc.stdout), ('stderr', proc.stderr))]
    threads.append(threading.Thread(target=feed, daemon=True))
    if cwd and max_file_bytes:
        threads.append(threading.Thread(target=watch, daemon=True))
    for t in threads:
        t.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop('timeout')
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
    if state['terminated']:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
    for t in threads:
        t.join(5)
    for pipe in (proc.stdout, proc.stderr):
        try:
            pipe.close()
        except OSError:
            pass
    return {'code': proc.returncode, 'terminated': state['terminated'],
            'stdout': b''.join(buffers['stdout']).decode('utf-8', 'replace'),
            'stderr': b''.join(buffers['stderr']).decode('utf-8', 'replace')}
