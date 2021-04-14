import subprocess


def run(*args, **kwargs):
    if isinstance(args[0], list):
        args = tuple(args[0])
    kwargs.setdefault('stdout', subprocess.PIPE)
    kwargs.setdefault('stderr', subprocess.PIPE)
    exception = kwargs.pop('exception', None)
    exception_message = kwargs.pop('exception_msg', None)
    check = kwargs.pop('check', True)
    shell = kwargs.pop('shell', False)
    proc = subprocess.Popen(args, stdout=kwargs['stdout'], stderr=kwargs['stderr'], shell=shell)
    stdout, stderr = proc.communicate()
    cp = subprocess.CompletedProcess(args, proc.returncode, stdout=stdout, stderr=stderr)
    if check:
        if cp.returncode and exception and exception_message:
            raise exception(exception_message)
        else:
            cp.check_returncode()
    return cp