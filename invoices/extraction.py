"""Bounded hybrid extraction with conservative, explainable candidates, never acceptance."""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import re
import shutil
import time
from django.conf import settings
from django.core.files import File
from django.core.exceptions import ValidationError
import pdfplumber
import pytesseract
import pypdfium2 as pdfium
from PIL import Image,ImageOps,ImageEnhance
from pytesseract import TesseractNotFoundError,TesseractError
from .file_security import validate_document
from .manual_calculations import money,decimal,calculate_invoice,validate_stored_totals,line_basis,discount_amount,allocate_discount,base_amounts,split_tax
from .gstin import gstin_evidence,ocr_gstin_candidate
ZERO=Decimal('0.00')


class OCRUnavailableError(Exception):pass


def _ocr_image(image,deadline=None):
    if image.width*image.height>settings.DOCUMENT_MAX_PIXELS:raise ValueError('Image exceeds the supported pixel limit.')
    image=ImageOps.exif_transpose(image).convert('RGB')
    available=shutil.which('tesseract')
    if available:
        try:
            orientation=pytesseract.image_to_osd(image,timeout=min(5,settings.OCR_TIMEOUT))
            rotation=re.search(r'Rotate:\s*(90|180|270)\b',orientation)
            if rotation:image=image.rotate(-int(rotation[1]),expand=True)
        except (RuntimeError,TesseractError,TesseractNotFoundError):pass
    image=ImageOps.autocontrast(ImageOps.grayscale(image))
    if image.width<1600:
        scale=min(3,max(1,1600//max(image.width,1)))
        if image.width*image.height*scale*scale<=settings.DOCUMENT_MAX_PIXELS and scale>1:image=image.resize((image.width*scale,image.height*scale),Image.Resampling.LANCZOS)
    image=ImageEnhance.Contrast(image).enhance(1.5)
    timeout=settings.OCR_TIMEOUT if deadline is None else min(settings.OCR_TIMEOUT,max(0.1,deadline-time.monotonic()))
    try:return pytesseract.image_to_string(image,config='--psm 6',timeout=timeout)
    except TesseractNotFoundError:
        raise OCRUnavailableError('Local Tesseract is unavailable. The original remains available for manual review or an explicit later retry.') from None


def _read_pages(file_path,strict=False):
    with open(file_path,'rb') as source:validate_document(File(source,name=Path(file_path).name))
    deadline=time.monotonic()+getattr(settings,'DOCUMENT_WORKER_TIMEOUT',75)-5
    pages=[];errors=[];ocr_count=0;total=0;position_count=0;table_cells=0
    def ocr(image):
        nonlocal ocr_count
        if ocr_count>=getattr(settings,'DOCUMENT_OCR_MAX_PAGES',5):raise RuntimeError('ocr_budget')
        if time.monotonic()>=deadline:raise RuntimeError('document_timeout')
        ocr_count+=1;return _ocr_image(image,deadline)
    def add(index,text,method,tables=None,positions=None,error=''):
        nonlocal total
        cap=min(getattr(settings,'DOCUMENT_PAGE_MAX_TEXT',20000),max(0,getattr(settings,'DOCUMENT_MAX_TEXT',150000)-total))
        if len(text)>cap:errors.append('Text resource limit reached; review the source for omitted content.')
        text=text[:cap];total+=len(text)
        pages.append({'page':index+1,'text':text,'method':method,'tables':tables or [],'positions':positions or [],'error':error})
    def failure(exc):
        if strict:raise exc
        category='ocr_unavailable' if isinstance(exc,OCRUnavailableError) else 'ocr_failed' if isinstance(exc,TesseractError) else 'ocr_timeout' if isinstance(exc,RuntimeError) else 'ocr_failed'
        errors.append({'ocr_unavailable':'Local Tesseract is unavailable; manual review or an explicit retry is required.','ocr_timeout':'OCR timed out or exceeded its page budget; review omitted pages.','ocr_failed':'OCR failed safely; review the original.'}[category]);return category
    if Path(file_path).suffix.lower()=='.pdf':
        raster=None
        try:
            with pdfplumber.open(file_path) as pdf:
                for index,page in enumerate(pdf.pages):
                    if time.monotonic()>=deadline:
                        add(index,'','skipped',error='document_timeout');errors.append('Document time budget reached.');continue
                    text=page.extract_text() or ''
                    if len(re.sub(r'\W','',text))>=20:
                        tables=page.extract_tables() or []
                        safe_tables=[]
                        if len(tables)>10:errors.append('Table limit reached; review omitted content in the source.')
                        for table in tables[:10]:
                            safe=[]
                            if len(table)>101:errors.append('Table row limit reached; review omitted content in the source.')
                            for row in table[:101]:
                                if table_cells+len(row)>5000:
                                    errors.append('Table cell limit reached; review omitted content in the source.');break
                                safe.append([str(c or '')[:200] for c in row[:20]]);table_cells+=len(row[:20])
                            if safe:safe_tables.append(safe)
                        words=page.extract_words()[:max(0,500-position_count)]
                        positions=[{key:(word[key][:100] if key=='text' else word[key]) for key in ('text','x0','top','x1','bottom')} for word in words];position_count+=len(positions)
                        add(index,text,'embedded',safe_tables,positions)
                    else:
                        if raster is None:raster=pdfium.PdfDocument(file_path)
                        p=raster[index];bitmap=None
                        try:
                            if p.get_width()*p.get_height()*4>settings.DOCUMENT_MAX_PIXELS:raise RuntimeError('pixel_limit')
                            bitmap=p.render(scale=2,draw_annots=False);text=ocr(bitmap.to_pil());add(index,text,'ocr')
                        except (OCRUnavailableError,RuntimeError,TesseractError,ValueError) as exc:add(index,text,'ocr_failed',error=failure(exc))
                        finally:
                            if bitmap:bitmap.close()
                            p.close()
        finally:
            if raster:raster.close()
        methods={p['method'] for p in pages}
        kind='digital_pdf' if methods=={'embedded'} else 'mixed_pdf' if 'embedded' in methods else 'scanned_pdf'
    else:
        with Image.open(file_path, formats=('PNG', 'JPEG')) as image:  # Upload validation admits only these.
            try:add(0,ocr(image.copy()),'ocr')
            except (OCRUnavailableError,RuntimeError,TesseractError,ValueError) as exc:add(0,'','ocr_failed',error=failure(exc))
        kind='image_ocr'
    if not any(p['text'].strip() for p in pages):errors.append('No usable text extracted; enter the document details manually.')
    return pages,kind,list(dict.fromkeys(errors))


def _document_text(file_path):
    pages,kind,_=_read_pages(file_path,strict=True)
    return '\n\f\n'.join(p['text'] for p in pages),kind


def date_candidates(raw):
    raw=raw.strip()
    if re.fullmatch(r'\d{1,2}[/-]\d{1,2}[/-]\d{4}',raw):
        a,b,year=map(int,re.split('[-/]',raw));values=[]
        for day,month in [(a,b),(b,a)]:
            try:value=datetime(year,month,day).date().isoformat()
            except ValueError:continue
            if value not in values:values.append(value)
        return values
    for fmt in ('%Y-%m-%d','%d %b %Y','%d %B %Y','%d-%b-%Y','%d-%B-%Y','%B %d, %Y','%b %d, %Y'):
        try:return [datetime.strptime(raw,fmt).date().isoformat()]
        except ValueError:pass
    return []


AMOUNT=r'([-+]?(?:\d[\d,]*)(?:\.\d{1,2})?)(?![\d.%A-Za-z])'
DATE=r'(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{1,2}[ -][A-Za-z]{3,9}[ -]\d{4}|[A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})'
AMOUNTS={'subtotal':r'Sub\s*Total','taxable_amount':r'Taxable\s*(?:Amount|Value)|Assessable\s+Value','cgst_amount':'CGST','sgst_amount':'SGST','igst_amount':'IGST','other_charges':r'Other\s+Charges?','rounding_adjustment':r'Round(?:ing)?\s*(?:Off|Adjustment)','total_amount':r'Grand\s+Total|Total\s+Invoice\s+Value|Invoice\s+Total|Net\s+Payable|Total\s+Amount','discount_amount':r'(?:Invoice\s+)?Discount'}


def financial_reconciliation(data):
    """Check available evidence without filling unknown extracted components."""
    components=('taxable_amount','cgst_amount','sgst_amount','igst_amount','other_charges','rounding_adjustment')
    values={}
    for field in (*components,'total_amount'):
        try:values[field]=decimal(data[field]) if data.get(field) is not None else None
        except ValidationError:values[field]=None
    known=[values[k] for k in components if values[k] is not None]
    subtotal=sum(known,ZERO) if known else None
    total=values['total_amount']
    residual=total-subtotal if total is not None and subtotal is not None else None
    missing=[k.replace('_',' ') for k in (*components[:-1],'total_amount') if values[k] is None]
    warnings=[];status='consistent'
    if missing:
        status='incomplete'
        amounts=(f' Extracted total INR {total:.2f}; known component subtotal INR {subtotal:.2f}; unresolved difference INR {residual:.2f}.'
            if residual is not None else '')
        warnings.append('Financial reconciliation is incomplete: missing or conflicting '+', '.join(missing)+'.'+amounts+' Review the original; unknown amounts have not been replaced with zero.')
    else:
        rounding=values['rounding_adjustment']
        if rounding is not None:
            warnings=validate_stored_totals(*(values[k] for k in components[:4]),values['other_charges']+rounding,total)
            # Keep one actionable mismatch notice rather than two copies of the same issue.
            warnings=[w for w in warnings if w!='Invoice total does not reconcile with saved components.']
            if abs(rounding)>1:
                warnings.append('Extracted round-off exceeds the supported +/- INR 1.00 limit; review the original.')
        else:
            # Structural tax/sign checks do not depend on whether round-off was printed.
            if any(values[k]<0 for k in (*components[:-1],'total_amount')):
                warnings.append('Financial values cannot be negative.')
            if values['igst_amount'] and (values['cgst_amount'] or values['sgst_amount']):
                warnings.append('CGST/SGST and IGST cannot both be charged on the same standard supply; review the source.')
        if residual and (rounding is not None or abs(residual)>1):
            warnings.append(f'Invoice total does not reconcile with saved components. Extracted total INR {total:.2f}; calculated component total INR {subtotal:.2f}; unexplained difference INR {residual:.2f}. '
                +('Round-off is unknown and the difference exceeds the supported +/- INR 1.00 limit. ' if rounding is None else '')
                +'Compare the original and correct the reviewed inputs; no extracted amount was replaced.')
        elif residual and rounding is None:
            status='incomplete'
            warnings.append(f'Financial reconciliation is incomplete: extracted total INR {total:.2f}; calculated component total INR {subtotal:.2f}; unexplained difference INR {residual:.2f}. Round-off is unknown; confirm it from the original rather than inferring an adjustment.')
        if warnings and status!='incomplete':status='mismatch'
    return {'status':status,'warnings':warnings,'component_total':subtotal,'residual':residual}


def line_financial_reconciliation(data):
    """Reconcile candidate lines without inventing missing calculation inputs."""
    lines=data.get('line_items') or []
    warnings=[];missing=[];conflicts=set();gross=[];calculated=None
    def number(obj,key):
        try:return decimal(obj[key]) if obj.get(key) is not None else None
        except ValidationError:return None
    def mismatch(message,*fields):
        warnings.append(message+' Review the original and line coverage; extracted values were not replaced.')
        conflicts.update(('line_items',*fields))
    if not lines:missing.append('reliable line items')
    for index,row in enumerate(lines,1):
        quantity,rate,printed=(number(row,k) for k in ('quantity','rate','taxable_amount'))
        if quantity is not None and rate is not None and quantity>0 and rate>=0:
            try:bound=money(quantity*rate)
            except ValidationError:
                missing.append(f'line {index} supported quantity/rate basis');bound=None
            if bound is not None:gross.append(bound)
            # Both inclusive tax and nonnegative discounts can only reduce this bound.
            if printed is not None and bound is not None and printed>bound:
                mismatch(f'Line {index} financial discrepancy: quantity {quantity} x rate INR {rate:.2f} gives a maximum taxable basis INR {bound:.2f}; extracted line taxable INR {printed:.2f} exceeds it by INR {printed-bound:.2f}.')
        for field in ('quantity','rate','discount','tax_rate'):
            if number(row,field) is None:missing.append(f'line {index} {field.replace("_"," ")}')
        for field in ('discount_type','tax_treatment'):
            if not row.get(field):missing.append(f'line {index} {field.replace("_"," ")}')
    header_base=number(data,'taxable_amount')
    if lines and len(gross)==len(lines) and header_base is not None and header_base>sum(gross,ZERO):
        bound=sum(gross,ZERO)
        mismatch(f'Header-versus-line financial discrepancy: extracted header taxable INR {header_base:.2f}; maximum basis from extracted quantities/rates INR {bound:.2f}; unexplained excess INR {header_base-bound:.2f}.','taxable_amount','total_amount')
    printed_bases=[number(row,'taxable_amount') for row in lines]
    if lines and all(value is not None for value in printed_bases) and header_base is not None and header_base>sum(printed_bases,ZERO):
        printed_sum=sum(printed_bases,ZERO)
        mismatch(f'Header-versus-line financial discrepancy: extracted header taxable INR {header_base:.2f}; sum of extracted line taxable values INR {printed_sum:.2f}; unexplained excess INR {header_base-printed_sum:.2f}.','taxable_amount','total_amount')
    for field in ('pricing_mode','supply_category','invoice_discount_type'):
        if not data.get(field):missing.append(field.replace('_',' '))
    if number(data,'invoice_discount_value') is None:missing.append('invoice discount value')
    if not missing:
        try:
            if data['supply_category']!='standard':raise ValidationError('Supply category requires independent tax review.')
            prepared=[line_basis(row['quantity'],row['rate'],row['discount'],row['tax_rate'],
                pricing_mode=data['pricing_mode'],discount_type=row['discount_type'],tax_treatment=row['tax_treatment'],allowed_rates=settings.GST_ALLOWED_RATES) for row in lines]
            eligible=[row['net_basis'] if row['tax_treatment']=='taxable' else ZERO for row in prepared]
            reduction=discount_amount(sum(eligible,ZERO),number(data,'invoice_discount_value'),data['invoice_discount_type'])
            shares=allocate_discount(eligible,reduction)
            amounts=[base_amounts(row['net_basis']-share,row['tax_rate'],data['pricing_mode']) for row,share in zip(prepared,shares)]
            calculated={key:sum((row[key] for row in amounts),ZERO) for key in ('taxable_amount','tax_amount','total_amount')}
            calculated['lines']=amounts
        except ValidationError as exc:missing.append('supported reviewed calculation inputs ('+'; '.join(exc.messages)+')')
    if calculated is not None:
        try:
            splits=[split_tax(row['tax_amount'],data.get('supplier_state_code'),data.get('place_of_supply'),data['supply_category']) for row in calculated['lines']]
            for key in ('cgst_amount','sgst_amount','igst_amount'):calculated[key]=sum((row[key] for row in splits),ZERO)
        except ValidationError:missing.append('explicit supported supplier state/place of supply for the GST split')
        for index,(row,expected) in enumerate(zip(lines,calculated['lines']),1):
            for field in ('taxable_amount','tax_amount','total_amount'):
                actual=number(row,field)
                if actual is not None and actual!=expected[field]:
                    mismatch(f'Line {index} financial discrepancy: extracted {field.replace("_"," ")} INR {actual:.2f}; calculated INR {expected[field]:.2f}; difference INR {actual-expected[field]:.2f}.')
        for field in ('taxable_amount',*(('cgst_amount','sgst_amount','igst_amount') if 'cgst_amount' in calculated else ())):
            actual=number(data,field)
            if actual is None:missing.append('header '+field.replace('_',' '))
            elif actual!=calculated[field]:
                mismatch(f'Header-versus-line financial discrepancy: extracted {field.replace("_"," ")} INR {actual:.2f}; calculated from extracted lines INR {calculated[field]:.2f}; difference INR {actual-calculated[field]:.2f}.',field)
        taxes=[number(data,key) for key in ('cgst_amount','sgst_amount','igst_amount')]
        if all(value is not None for value in taxes) and sum(taxes,ZERO)!=calculated['tax_amount']:
            mismatch(f'Header-versus-line financial discrepancy: extracted GST total INR {sum(taxes,ZERO):.2f}; calculated from extracted lines INR {calculated["tax_amount"]:.2f}.','cgst_amount','sgst_amount','igst_amount')
        elif any(value is None for value in taxes):missing.append('header GST components')
        charges,rounding,total=(number(data,k) for k in ('other_charges','rounding_adjustment','total_amount'))
        if charges is None or total is None:missing.append('header charges/total needed for line-to-grand-total comparison')
        else:
            before_rounding=calculated['total_amount']+charges
            expected=before_rounding+(rounding if rounding is not None else ZERO)
            residual=total-expected
            if residual and (rounding is not None or abs(residual)>1):
                mismatch(f'Header-versus-line financial discrepancy: extracted total INR {total:.2f}; line-derived component total INR {expected:.2f}; unexplained difference INR {residual:.2f}.','total_amount')
            elif residual and rounding is None:missing.append('explicit round-off for the line-to-total residual')
    if missing:
        warnings.append('Line-item financial reconciliation is incomplete: missing, conflicting or unsupported '+', '.join(dict.fromkeys(missing))+'. Pricing, discounts, tax rates and round-off have not been inferred; compare all lines with the original.')
    return {'status':'mismatch' if conflicts else 'incomplete' if missing else 'consistent',
        'warnings':warnings,'conflicting_fields':sorted(conflicts),
        'calculated_taxable':calculated['taxable_amount'] if calculated is not None else None,
        'calculated_tax':calculated['tax_amount'] if calculated is not None else None,
        'calculated_total_before_rounding':calculated['total_amount'] if calculated is not None else None}


def parse_document(pages,kind,errors=()):
    data={'pipeline_version':9,'document_type':kind,'extracted_text':'\n\f\n'.join(p['text'] for p in pages),'pages':pages,'confidence':{},'line_items':[], 'validation_errors':list(errors)}
    confidence=data['confidence']
    def choose(field,pattern,convert=lambda v:v.strip()):
        found=[]
        for page in pages:
            for match in re.finditer(pattern,page['text'],re.I|re.M):
                try:value=convert(match[1])
                except (ValueError,ValidationError):continue
                found.append((value,page['page'],page['method']))
        values=[]
        for value,_,_ in found:
            if value not in values:values.append(value)
        value=values[0] if len(values)==1 else None
        level='conflicting' if len(values)>1 else 'missing' if not values else 'medium' if any(method!='embedded' for _,_,method in found) else 'high'
        confidence[field]={'level':level,'reason':'Different labelled candidates require review.' if len(values)>1 else 'No reliable labelled value found.' if not values else 'Found beside a matching label; OCR requires visual review.' if level=='medium' else 'Consistent value found beside a matching text label.','candidates':[str(v) for v in values],'pages':sorted({p for _,p,_ in found})}
        data[field]=value;return value
    for role,labels in [('supplier','Supplier|Seller|Sold By'),('buyer','Buyer|Bill To')]:
        choose(role+'_name',rf'^\s*(?:{labels})(?:\s+Name)?\s*:\s*([^\n]+)')
        choose(role+'_address',rf'^\s*(?:{role})\s+Address\s*:\s*([^\n]+)')
        choose(role+'_gstin',rf'\b(?:{role}|'+('Seller' if role=='supplier' else 'Buyer')+r')\s+GSTIN\s*[:#-]?\s*([A-Za-z0-9 ]{10,25})(?=\n|$)',lambda v:re.sub(r'\s+','',v).upper())
        # A generic GSTIN is assigned only within an explicitly labelled party block.
        if data[role+'_gstin'] is None and confidence[role+'_gstin']['level']=='missing':
            candidates=[]
            for page in pages:
                block=re.search(rf'(?:^|\n)\s*(?:{labels})(?:\s+Name)?\s*:[\s\S]*?(?=\n\s*(?:Buyer|Bill To|Supplier|Seller|Invoice|Taxable|Grand Total)\b|\Z)',page['text'],re.I)
                if block:
                    candidates.extend(re.findall(r'\bGSTIN\s*[:#-]?\s*([A-Za-z0-9]{10,20})',block[0],re.I))
            unique=list(dict.fromkeys(c.upper() for c in candidates))
            if len(unique)==1:data[role+'_gstin']=unique[0];confidence[role+'_gstin'].update(level='medium',reason='Found in an explicitly labelled party block.',candidates=unique)
        if data[role+'_gstin']:
            field=role+'_gstin'
            original=data[field]
            # Embedded text and conflicting candidates are not OCR repair evidence.
            ocr_pages=confidence[field].get('pages',[])
            from_ocr=bool(ocr_pages) and all(p['method']!='embedded' for p in pages if p['page'] in ocr_pages)
            candidate=ocr_gstin_candidate(original) if from_ocr else None
            if candidate:
                data[field+'_ocr_correction']={'original':original,'candidate':candidate,'review_required':True,'registration':'not_verified'}
                data[field]=candidate
                confidence[field].update(level='low',reason='Position-aware 0/O OCR candidate passes local structure/state/checksum only; compare with the source before confirming.')
                data['validation_errors'].append(f'{role.title()} GSTIN OCR candidate: {original} → {candidate}. Human review required; local checksum is not official registration verification.')
            evidence=gstin_evidence(data[role+'_gstin']);data[role+'_gstin_validation']=evidence
            if not evidence['checksum_valid']:
                confidence[role+'_gstin'].update(level='low',reason='GSTIN structure, state or checksum failed; no character repair was attempted.')
                data['validation_errors'].append(role.title()+' GSTIN requires correction; local validation failed.')
    choose('invoice_number',r'(?:\bInvoice|^[ \t]*Bill)[ \t]*(?:Number|No\.?|#)[ \t]*[:#-]?[ \t]*([^\n]+)',lambda v:re.split(r'\s{2,}|\s+(?:Invoice\s+Date|Date|PO\s*(?:No|Number))\b',v.strip(),1,flags=re.I)[0][:100])
    if data['invoice_number'] and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 /_.-]{0,99}',data['invoice_number']):
        data['invoice_number']=None;confidence['invoice_number'].update(level='low',reason='The labelled candidate is incomplete or resembles another field; enter the actual identifier.')
    if data['invoice_number'] and (re.fullmatch(r'[A-F0-9]{64}',data['invoice_number'],re.I) or gstin_evidence(data['invoice_number'])['structural_valid']):
        data['invoice_number']=None;confidence['invoice_number'].update(level='low',reason='Candidate resembles a GSTIN/IRN rather than an invoice number.')
    choose('purchase_order',r'\b(?:Purchase\s+Order|PO)\s*(?:No\.?|Number|#)?\s*[:#]\s*([^\n]+)')
    for field,label in [('invoice_date',r'Invoice[ \t]+Date\b|Bill[ \t]+Date\b|Date(?=[ \t]*[:#=])'),('due_date',r'Due[ \t]+Date\b')]:
        found=[]
        for page in pages:
            for match in re.finditer(rf'^[ \t]*(?:{label})[ \t]*[:#=-]?[ \t]*([^\n]+)',page['text'],re.I|re.M):
                token=re.match(DATE+r'(?![\w/-])',match[1].strip())
                raw=token[1] if token else match[1].strip()
                found.append((raw,page['page'],page['method'],date_candidates(raw)))
        candidates=list(dict.fromkeys(value for *_,values in found for value in values))
        invalid=any(not values for *_,values in found)
        reliable=len(candidates)==1 and not invalid
        data[field]=candidates[0] if reliable else None
        level=('medium' if any(method!='embedded' for _,_,method,_ in found) else 'high') if reliable else 'conflicting' if len(candidates)>1 or (candidates and invalid) else 'low' if found else 'missing'
        confidence[field]={'level':level,'reason':'Consistent labelled date candidate; human review is required.' if reliable else 'Date is missing, invalid, ambiguous or conflicting; select the actual date from the original.',
            'candidates':candidates,'raw_candidates':list(dict.fromkeys(raw for raw,*_ in found)),'pages':sorted({page for _,page,_,_ in found})}
        if found and not reliable:data['validation_errors'].append(field.replace('_',' ').title()+' requires explicit date review.')
    for field,label in AMOUNTS.items():
        choose(field,rf'\b(?:{label})\s*(?:@?\s*\d+(?:\.\d+)?\s*%)?\s*[:=]?\s*(?:INR|Rs\.?|₹)?\s*{AMOUNT}',lambda v:money(v.replace(',','')))
    choose('supplier_state_code',r'\bSupplier\s+State(?:\s+Code)?\s*:\s*(\d{2})\b')
    choose('place_of_supply',r'\bPlace\s+of\s+Supply\s*:\s*(?:[A-Za-z ]*\(?\s*)?(\d{2})\b')
    choose('pricing_mode',r'\b(?:Pricing(?:\s+Mode)?|GST\s+Basis)\s*:\s*(inclusive|exclusive)\b',str.lower)
    choose('supply_category',r'\bSupply\s+Category\s*:\s*(standard|reverse_charge|export|sez)\b',str.lower)
    choose('detected_document_kind',r'^\s*(CREDIT NOTE|DEBIT NOTE|PROFORMA INVOICE|QUOTATION|TAX INVOICE|SALES INVOICE|PURCHASE INVOICE)\s*$',str.lower)
    if re.search(r'Reverse\s+Charge\s*[:=-]\s*(?:Yes|True|Applicable)\b',data['extracted_text'],re.I):
        data['supply_category']='reverse_charge';confidence['supply_category'].update(level='medium',reason='Document explicitly indicates reverse charge; automatic acceptance is unsupported.')
    data['unassigned_gstins']=sorted(set(re.findall(r'\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b',data['extracted_text'].upper()))-{data.get('supplier_gstin'),data.get('buyer_gstin')})
    data['invoice_discount_type']='fixed';data['invoice_discount_value']=data.get('discount_amount')
    discount_percent=re.search(r'\b(?:Invoice\s+)?Discount\s*:\s*(\d+(?:\.\d+)?)\s*%',data['extracted_text'],re.I)
    if discount_percent:data.update(invoice_discount_type='percent',invoice_discount_value=discount_percent[1],discount_amount=None)
    data['line_items']=extract_lines(pages)
    if len(data['line_items'])==100:data['validation_errors'].append('The 100-line review limit was reached; check the original for omitted lines.')
    data['confidence']['line_items']={'level':'medium' if data['line_items'] else 'missing','reason':'Recognized table/row labels; every line still needs human review.' if data['line_items'] else 'No supported, reliable line table found.'}
    required=('invoice_number','invoice_date','supplier_name','taxable_amount','cgst_amount','sgst_amount','igst_amount','total_amount')
    for field in required:
        if data.get(field) is None:data['validation_errors'].append(field.replace('_',' ').title()+' is unknown or conflicting; review required.')
    if not data['line_items']:data['validation_errors'].append('No reliable line items; add them from the original.')
    reconciliation=financial_reconciliation(data)
    data['financial_reconciliation']=reconciliation
    data['validation_errors'].extend(reconciliation['warnings'])
    if reconciliation['warnings']:
        level='conflicting' if reconciliation['status']=='mismatch' or confidence['total_amount']['level']=='conflicting' else 'low'
        if confidence['total_amount']['level']=='missing':level='missing'
        confidence['total_amount'].update(level=level,reason=' '.join(reconciliation['warnings']))
    line_check=line_financial_reconciliation(data)
    data['line_financial_reconciliation']=line_check
    data['validation_errors'].extend(line_check['warnings'])
    if line_check['warnings']:
        reason='Line financial reconciliation '+line_check['status']+'. See current financial warnings and compare the original; extracted values were retained.'
        for field in set(line_check['conflicting_fields'])|{'line_items','total_amount'}:
            if field not in confidence:continue
            prior=confidence[field]['level']
            level='conflicting' if field in line_check['conflicting_fields'] or prior=='conflicting' else 'missing' if prior=='missing' else 'low'
            confidence[field].update(level=level,reason=reason)
    data['validation_errors'].append('GST pricing, supply, classification and line inputs must be confirmed; extraction never verifies an invoice.')
    return data


def extract_lines(pages):
    aliases={'description':'description','item':'description','particulars':'description','hsnsac':'hsn_sac_code','hsn':'hsn_sac_code','quantity':'quantity','qty':'quantity','unit':'unit','uom':'unit','unitprice':'rate','rate':'rate','discount':'discount','gstrate':'tax_rate','gst':'tax_rate','taxablevalue':'taxable_amount','taxableamount':'taxable_amount','total':'total_amount','linetotal':'total_amount','taxamount':'tax_amount','taxtreatment':'tax_treatment'}
    output=[]
    for page in pages:
        found=[]
        for table in page.get('tables',[]):
            if not table:continue
            columns={index:aliases.get(re.sub('[^a-z]','',str(value).lower())) for index,value in enumerate(table[0])}
            if not {'description','quantity','rate'}<=set(columns.values()):continue
            for row in table[1:]:
                values={field:row[index] for index,field in columns.items() if field and index<len(row)}
                if not values.get('description') or values['description'].lower() in {'total','grand total','subtotal'}:continue
                found.append(values)
        if not found:
            pattern=r'^\s*\d+\s+(.+?)\s+(\d{4,8})\s+(\d+(?:\.\d+)?)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+(\d+(?:\.\d+)?)%\s*$'
            for match in re.finditer(pattern,page['text'],re.M):
                description,hsn,qty,rate,base,tax=match.groups();found.append(dict(description=description,hsn_sac_code=hsn,quantity=qty,rate=rate,taxable_amount=base,tax_rate=tax))
        for values in found:
            item={'description':str(values.get('description',''))[:500],'hsn_sac_code':str(values.get('hsn_sac_code',''))[:20],'unit':str(values.get('unit',''))[:30],'page':page['page']}
            for field in ('quantity','rate','discount','tax_rate','taxable_amount','tax_amount','total_amount'):
                try:item[field]=str(Decimal(str(values[field]).replace(',','').replace('%','').strip())) if values.get(field) not in (None,'') else None
                except Exception:item[field]=None
                if item[field] is not None and (not Decimal(item[field]).is_finite() or Decimal(item[field])<0):item[field]=None
            treatment=str(values.get('tax_treatment','')).lower().replace('-','_')
            item['tax_treatment']=treatment if treatment in {'taxable','exempt','nil_rated','non_gst'} else 'taxable' if item.get('tax_rate') and Decimal(item['tax_rate'])>0 else None
            item['discount_type']='percent' if '%' in str(values.get('discount','')) else 'fixed' if item.get('discount') is not None else None
            output.append(item)
            if len(output)>=100:return output
    return output


def _extract_local(file_path):
    start=time.monotonic();pages,kind,errors=_read_pages(file_path);data=parse_document(pages,kind,errors)
    data['elapsed_seconds']=round(time.monotonic()-start,4)
    data['engine']={'parser':'rules-v9','pdfplumber':pdfplumber.__version__,'tesseract_available':bool(shutil.which('tesseract')),'ocr_version':None}
    if data['engine']['tesseract_available']:
        try:data['engine']['ocr_version']=str(pytesseract.get_tesseract_version())[:80]
        except Exception:pass
    return data


def extract_invoice_data(file_path):
    from .document_worker import run_worker
    with open(file_path,'rb') as source:return run_worker(File(source,name=Path(file_path).name),'extract')
