"""Run individual in-memory pytest cases with no network, DNS or child-process calls."""
import sys


def deny_network():
    """The Linux hosted lane denies network endpoints before importing any packages."""
    import ctypes as c
    import errno

    if sys.platform != 'linux':
        raise SystemExit('The offline CI runner requires Linux and libseccomp')
    lib = c.CDLL('libseccomp.so.2')
    lib.seccomp_init.argtypes, lib.seccomp_init.restype = [c.c_uint32], c.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [c.c_char_p]
    lib.seccomp_rule_add.argtypes = [c.c_void_p, c.c_uint32, c.c_int, c.c_uint]
    lib.seccomp_load.argtypes = lib.seccomp_release.argtypes = [c.c_void_p]
    lib.seccomp_release.restype = None
    policy = lib.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    if not policy:
        raise SystemExit('Could not allocate the offline CI policy')
    try:
        for name in (b'socket', b'connect', b'bind', b'listen', b'accept', b'accept4'):
            call = lib.seccomp_syscall_resolve_name(name)
            if call < 0 or lib.seccomp_rule_add(policy, 0x00050000 | errno.EPERM, call, 0):  # SCMP_ACT_ERRNO
                raise SystemExit(f'Could not add the offline rule for {name.decode()}')
        if lib.seccomp_load(policy):
            raise SystemExit('Could not install the offline CI policy')
    finally:
        lib.seccomp_release(policy)


def main(args):
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit('Use python -I -S -B for the offline test process')
    if '--' not in args:
        raise SystemExit('Expected installed library paths, --, then explicit pytest node IDs')
    i = args.index('--')
    libraries, nodes = args[:i], args[i + 1:]
    selectors = [node.partition('::') for node in nodes]
    if not libraries or not selectors or any(
            not separator or not case or not path.endswith('.py') or path.startswith('-')
            for path, separator, case in selectors):
        raise SystemExit('Offline tests require explicit path.py::case selectors')

    deny_network()
    import socket

    attempts = []
    child_events = {'subprocess.Popen', 'os.system', 'os.exec', 'os.spawn', 'os.posix_spawn', 'os.fork', 'os.forkpty'}

    def offline(event, _args):
        if event == 'socket.__new__' and _args[1] == socket.AF_UNIX:
            return  # asyncio uses a private in-process socketpair, with no bind, connect or lookup
        if event.startswith('socket.') or event in child_events:
            attempts.append(event)
            raise PermissionError('Offline test attempted: ' + event)

    # Install the guard before site-packages, pytest plugins or payload code can run.
    sys.addaudithook(offline)
    socket.has_ipv6 = False  # the in-memory fixtures do not need dependency-level IPv6 probing
    import os
    from pathlib import Path
    from tempfile import TemporaryDirectory

    with TemporaryDirectory(prefix='hub-offline-pytest-') as scratch:
        os.environ.clear()
        os.environ.update(HOME=scratch, USERPROFILE=scratch, TMPDIR=scratch, TEMP=scratch,
                          NO_PROXY='*', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
        sys.path.extend(libraries)  # use installed packages without executing .pth files
        sys.path.append(str(Path(__file__).resolve().parents[1]))
        import pytest

        result = pytest.main(['-c', os.devnull, '--noconftest', '-p', 'no:cacheprovider', '-q', '-rs',
                              '--basetemp', os.path.join(scratch, 'cases'), *nodes])
    if attempts:
        print('Offline test operations refused: ' + ', '.join(sorted(set(attempts))), file=sys.stderr)
        return 1
    if not result:
        print(f'Offline check: {len(nodes)} explicit node IDs, no socket, DNS or process attempts.')
    return int(result)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
