"""Isolated local document work; no database, credentials or external service access."""
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from django.conf import settings
from django.core.exceptions import ValidationError

MAX_BYTES = 10 * 1024 * 1024
LIMITS = {'DOCUMENT_MAX_PAGES':50, 'DOCUMENT_MAX_PIXELS':25_000_000,
          'OCR_TIMEOUT':30, 'DOCUMENT_WORKER_TIMEOUT':75, 'DOCUMENT_OCR_MAX_PAGES':5,
          'DOCUMENT_MAX_TEXT':150_000, 'DOCUMENT_PAGE_MAX_TEXT':20_000}


@contextlib.contextmanager
def worker_slot():
    import fcntl
    root=Path(tempfile.gettempdir())/f'smart-invoice-document-workers-{os.getuid()}'
    root.mkdir(mode=0o700,exist_ok=True)
    if root.is_symlink() or root.stat().st_uid!=os.getuid():
        raise ValidationError('Document processing is temporarily unavailable.')
    handle=None
    for index in range(2):
        fd=os.open(root/str(index),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        candidate=os.fdopen(fd,'a')
        try:fcntl.flock(candidate,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:candidate.close();continue
        handle=candidate;break
    if handle is None:raise ValidationError('Document processing is busy. Try again shortly; no automatic retry occurs.')
    try:yield handle
    finally:handle.close()


def run_worker(upload, mode):
    """Materialize bounded bytes, isolate native parsers and kill the process group on timeout."""
    suffix=Path(upload.name).suffix.lower()
    if suffix not in {'.pdf','.png','.jpg','.jpeg'}:raise ValidationError('Upload a PDF, PNG, JPG or JPEG file.')
    config={name:getattr(settings,name,default) for name,default in LIMITS.items()}
    config['GST_ALLOWED_RATES']=list(settings.GST_ALLOWED_RATES)
    with tempfile.TemporaryDirectory(prefix='smart-invoice-document-') as directory:
        path=Path(directory)/('source'+suffix);size=0
        try:
            upload.seek(0)
            with path.open('xb') as output:
                for chunk in upload.chunks():
                    size+=len(chunk)
                    if size>MAX_BYTES:raise ValidationError('File size must not exceed 10 MB.')
                    output.write(chunk)
        finally:upload.seek(0)
        if not size:raise ValidationError('The document is empty.')
        env={key:os.environ[key] for key in ('PATH','LANG','LC_ALL','TMPDIR','TESSDATA_PREFIX') if key in os.environ}
        env.update(PYTHONPATH=str(Path(__file__).resolve().parent.parent),OMP_THREAD_LIMIT='1',OPENBLAS_NUM_THREADS='1',TMPDIR=directory,TMP=directory,TEMP=directory)
        with worker_slot() as lease:
            process=subprocess.Popen([sys.executable,'-m','invoices.document_worker',mode,str(path),json.dumps(config)],
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,start_new_session=True,pass_fds=(lease.fileno(),))
            try:output,_=process.communicate(timeout=config['DOCUMENT_WORKER_TIMEOUT'])
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.communicate()
                raise ValidationError('Document processing timed out. The source can be reviewed manually.') from None
        if process.returncode or len(output)>2_000_000:
            raise ValidationError('Document processing exceeded its resource limits or failed safely.')
        try:result=json.loads(output)
        except (ValueError,TypeError):raise ValidationError('Document processing failed safely.') from None
        if result.get('error'):raise ValidationError(result['error'])
        return result['result']


def main():
    from django.conf import settings as worker_settings
    config=json.loads(sys.argv[3]);worker_settings.configure(**config,EXTRACTION_WORKER=True)
    # The child retains its lease if the HTTP parent disappears, and has its own wall clock.
    def wall_limit(*_):
        if os.getpgrp()==os.getpid():os.killpg(os.getpid(),signal.SIGKILL)
        os._exit(124)
    signal.signal(signal.SIGALRM,wall_limit)
    signal.setitimer(signal.ITIMER_REAL,config['DOCUMENT_WORKER_TIMEOUT']+1)
    # Linux enforces an address-space ceiling. macOS address-space limits are not reliable.
    import resource
    resource.setrlimit(resource.RLIMIT_CPU,(60,60))
    if sys.platform.startswith('linux'):resource.setrlimit(resource.RLIMIT_AS,(1024**3,1024**3))
    try:
        from django.core.files import File
        from .file_security import _validate_local
        if sys.argv[1]=='validate':
            with open(sys.argv[2],'rb') as source:_validate_local(File(source,name=Path(sys.argv[2]).name))
            result={'validated':True}
        else:
            from .extraction import _extract_local
            result=_extract_local(sys.argv[2])
        print(json.dumps({'result':result},default=str))
    except ValidationError as exc:print(json.dumps({'error':' '.join(exc.messages)}))
    except Exception:print(json.dumps({'error':'The document is malformed, unsupported or could not be processed safely.'}))


if __name__=='__main__':main()
