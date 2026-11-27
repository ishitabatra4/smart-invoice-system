"""Export immutable saved evidence, never a newly calculated or filed return."""
import csv
import re
from io import BytesIO, StringIO
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.db.models import Q
from audit.models import AuditEvent
from config.export_security import spreadsheet_row


def flatten(value, path=''):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from flatten(item, f'{path}.{key}' if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from flatten(item, f'{path}[{index}]')
        if not value:
            yield path, '[]'
    else:
        yield path, '' if value is None else str(value)



SECTIONS = {'gstr1': 'gstr1', 'gstr3b': 'gstr3b', 'itc': 'reconciliation', 'liability': 'liability', 'income_summary': 'income_summary', 'income_tax': 'income_tax'}


def pdf_fields(value, path=''):
    """Keep each saved list item together instead of making a row per leaf."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from pdf_fields(item, f'{path}.{key}' if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield f'{path}[{index}]', '; '.join(f'{key}: {cell}' if key else str(cell) for key, cell in flatten(item))
        if not value:
            yield path, '[]'
    else:
        yield path, '' if value is None else str(value)


def summary_rows(paper, *, compact=False):
    """Presentation of frozen data only; no source services or current totals."""
    data = paper.data
    period = data.get('period', {})
    rows = [('Report', data.get('title', paper.get_kind_display())),
        ('Tax period start', period.get('start', '')), ('Tax period end', period.get('end', '')),
        ('Working paper', str(paper.pk)), ('Revision', str(paper.revision)),
        ('Calculation version', data.get('calculation_version', paper.calculation_version)),
        ('Scope', 'Preparation only; not filed, authenticated portal data or a statutory payment determination.')]
    for key, value in (pdf_fields if compact else flatten)(data.get(SECTIONS[paper.kind])):
        label = re.sub(r'\[(\d+)\]', r' / row \1', key).replace('.', ' / ').replace('_', ' ')
        label = label.replace('taxable', 'value before GST').replace('cgst', 'CGST').replace('sgst', 'SGST').replace('igst', 'IGST')
        rows.append((label, value))
    for index, source in enumerate(data.get('included', []), 1):
        prefix = f'Included record {index}'
        fields = [('ID', str(source['id'])), ('Number', source.get('number','')), ('Date', source.get('date','')), ('Supplier', source.get('supplier_name','')), ('Buyer', source.get('buyer_name',''))]
        fields.extend((key.upper(), value) for key,value in source.get('values',{}).items())
        if compact:
            rows.append((prefix, '; '.join(f'{key}: {value}' for key, value in fields)))
        else:
            rows.extend((prefix+' / '+key, value) for key,value in fields)
    rows.extend(('Warning / '+str(w.get('severity','review')), w.get('message','')) for w in data.get('warnings',[]))
    return rows


def validate_excel_row(row):
    if any(len(str(cell)) > 32767 or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', str(cell)) for cell in row):
        raise ValidationError('A saved field exceeds Excel text/control-character limits; use CSV. No content was truncated.')


def excel_row(sheet, row):
    sheet.append(spreadsheet_row(row))


def export_paper(paper, fmt):
    evidence = {'working_paper_id': paper.pk, 'revision': paper.revision, 'status': paper.status, 'prepared_at': paper.prepared_at.isoformat(), 'source_fingerprint': paper.source_fingerprint, 'rule_fingerprint': paper.rule_fingerprint, 'inputs': paper.inputs, 'snapshot': paper.data, 'lifecycle': [{'status': e.status, 'actor_id': e.actor_id, 'reason': e.reason, 'at': e.created_at.isoformat()} for e in paper.events.order_by('pk')]}
    audit = AuditEvent.objects.filter(business_id=paper.period.business_id, user_id=paper.prepared_by_id, module='taxation').filter(
        Q(object_type='taxation.taxworkingpaper', object_id=str(paper.pk)) |
        Q(object_type='taxation.taxperiod', object_id=str(paper.period_id))).order_by('pk')
    if audit.count() > 1000:
        raise ValidationError('This working paper exceeds the 1,000-event audit export limit; use the existing audit reporting workflow.')
    evidence['audit_at_export'] = list(audit.values('pk', 'action', 'object_type', 'object_id', 'description', 'created_at', 'metadata', 'success'))
    evidence['audit_scope'] = 'Owner-scoped period and working-paper events before this export; append-only history, separate from the frozen financial body.'
    summary = summary_rows(paper)
    rows = [('Field', 'Saved value'), ('Section', 'Readable summary'), *summary, ('Section', 'Complete saved evidence and detail rows'), *flatten(evidence)]
    if fmt == 'csv':
        stream = StringIO(newline=''); writer = csv.writer(stream)
        writer.writerows(spreadsheet_row(row) for row in rows)
        content, mime = '\ufeff' + stream.getvalue(), 'text/csv; charset=utf-8'
    elif fmt == 'xlsx':
        from openpyxl import Workbook
        # Validate before opening write-only XML streams, including failure paths.
        for row in rows:
            validate_excel_row(row)
        for row in summary:
            validate_excel_row(row)
        workbook = Workbook(write_only=True); sheet = workbook.create_sheet('Saved working paper')
        for row in rows:
            excel_row(sheet, row)
        summary_sheet = workbook.create_sheet('Readable summary')
        excel_row(summary_sheet, ('Report field', 'Saved value'))
        for row in summary:
            excel_row(summary_sheet, row)
        # Keep the existing complete-evidence sheet first for compatibility.

        stream = BytesIO(); workbook.save(stream)
        content, mime = stream.getvalue(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    elif fmt == 'pdf':
        from invoices.pdf_generator import render_report_pdf, PDFGenerationError
        pdf_rows = summary_rows(paper, compact=True) + list(pdf_fields({'excluded':paper.data.get('excluded',[]), 'lifecycle':evidence['lifecycle'], 'references':paper.data['rule_bundle']['references'], 'audit_event_count':len(evidence['audit_at_export'])}))
        try:
            content = render_report_pdf(paper.data['title'], ['Report field', 'Saved value'], pdf_rows,
                'Working paper summary, not a filed return or government payable. Complete frozen source/rule evidence is available in CSV/Excel. ' +
                'Owner-reviewed ITC is not statutory verification. User imports are not authenticated portal evidence.')
        except PDFGenerationError as exc:
            raise ValidationError('PDF summary exceeds supported text/page/row limits; use CSV/Excel or a shorter period.') from exc
        mime = 'application/pdf'
    else:
        raise ValidationError('Choose CSV, Excel or PDF.')
    response = HttpResponse(content, content_type=mime)
    response['Content-Disposition'] = f'attachment; filename="tax-working-{paper.pk}-r{paper.revision}.{fmt}"'
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response
