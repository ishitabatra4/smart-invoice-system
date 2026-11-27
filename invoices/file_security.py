"""Validate real formats in bounded workers; never execute or fetch document content."""
from pathlib import Path
import warnings
from django.conf import settings
from django.core.exceptions import ValidationError
import pypdfium2 as pdfium
from PIL import Image


def validate_document(upload):
    if getattr(settings,'EXTRACTION_WORKER',False):return _validate_local(upload)
    from .document_worker import run_worker
    run_worker(upload,'validate')


def _validate_local(upload):
    from pdfminer.pdftypes import resolve1
    from pdfminer.pdfdocument import PDFPasswordIncorrect
    import pdfplumber
    suffix=Path(upload.name).suffix.lower()
    if suffix not in {'.pdf','.png','.jpg','.jpeg'}:raise ValidationError('Upload a PDF, JPG, JPEG or PNG file.')
    if upload.size>10*1024*1024:raise ValidationError('File size must not exceed 10 MB.')
    def name(value):return getattr(value,'name',str(value))
    def active(obj,depth=0):
        if depth>6:return False
        obj=resolve1(obj)
        if isinstance(obj,dict):
            keys={name(k) for k in obj}
            if keys & {'JavaScript','JS','EmbeddedFiles','XFA','AcroForm','OpenAction','AA'}:return True
            if name(obj.get('S')) in {'JavaScript','Launch','GoToR','GoToE','SubmitForm','ImportData'}:return True
            if name(obj.get('Subtype')) in {'RichMedia','FileAttachment','Movie','Sound'}:return True
            return any(active(v,depth+1) for k,v in obj.items() if name(k) in {'Names','A','AcroForm'})
        if isinstance(obj,list):return any(active(v,depth+1) for v in obj[:1000])
        return False
    try:
        upload.seek(0)
        if suffix=='.pdf':
            content=upload.read(10*1024*1024+1)
            if not content.startswith(b'%PDF-') or len(content)>10*1024*1024:raise ValueError
            from io import BytesIO
            with pdfplumber.open(BytesIO(content)) as pdf:
                if pdf.doc.encryption is not None:raise ValidationError('Password-protected PDFs are not supported.')
                seen=set()
                for xref in pdf.doc.xrefs:
                    for objid in xref.get_objids():
                        if objid in seen:continue
                        seen.add(objid)
                        if len(seen)>10000:raise ValidationError('PDF exceeds the supported object limit.')
                        obj=pdf.doc.getobj(objid)
                        attrs=getattr(obj,'attrs',obj)
                        if isinstance(attrs,dict) and (name(attrs.get('Type')) in {'Filespec','EmbeddedFile'} or (hasattr(obj,'attrs') and 'F' in attrs) or active(attrs)):
                            raise ValidationError('PDF active content, attachments or external file references are not supported.')
                if active(pdf.doc.catalog):raise ValidationError('PDF scripts, actions, forms or embedded attachments are not supported.')
                if not 0<len(pdf.pages)<=settings.DOCUMENT_MAX_PAGES:raise ValidationError('PDF exceeds the supported page limit.')
                for page in pdf.pages:
                    if page.width<=0 or page.height<=0 or page.width*page.height*4>settings.DOCUMENT_MAX_PIXELS:raise ValidationError('PDF page exceeds the supported pixel limit.')
                    if active(page.page_obj.attrs):raise ValidationError('PDF page actions are not supported.')
                    annotations=resolve1(page.page_obj.attrs.get('Annots',[]))
                    if len(annotations or [])>1000 or active(annotations):raise ValidationError('PDF active or excessive annotations are not supported.')
        else:
            with warnings.catch_warnings():
                warnings.simplefilter('error',Image.DecompressionBombWarning)
                with Image.open(upload, formats=('PNG', 'JPEG')) as image:  # Never reach other Pillow decoders.
                    if image.format!=('PNG' if suffix=='.png' else 'JPEG'):raise ValueError
                    if image.width*image.height>settings.DOCUMENT_MAX_PIXELS or max(image.size)>12000 or getattr(image,'n_frames',1)!=1:raise ValidationError('Image exceeds the supported pixel, dimension or frame limit.')
                    image.verify()
    except ValidationError:raise
    except PDFPasswordIncorrect:raise ValidationError('Password-protected PDFs are not supported.') from None
    except Exception:raise ValidationError('The file is malformed or its contents do not match the selected file type.') from None
    finally:upload.seek(0)
