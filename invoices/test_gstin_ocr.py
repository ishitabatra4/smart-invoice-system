"""Local OCR suggestions retain evidence and never establish registration."""
from copy import deepcopy
from django.test import SimpleTestCase
from .gstin import ocr_gstin_candidate,gstin_evidence,ALPHABET
from .extraction import parse_document

class GSTINOCRTests(SimpleTestCase):
    def parse(self,value,method='ocr',extra=''):
        return parse_document([{'page':1,'method':method,'text':'Supplier GSTIN: '+value+'\n'+extra,'tables':[]}],'image_ocr')

    def test_exact_zero_o_confusions(self):
        for raw,expected in [('O8ABCDE1234F1Z0','08ABCDE1234F1Z0'),('O8ABCDE91OOF1ZV','08ABCDE9100F1ZV'),('08ABCDE1234F1ZO','08ABCDE1234F1Z0')]:
            with self.subTest(raw=raw):
                self.assertEqual(ocr_gstin_candidate(raw),expected)
                data=self.parse(raw)
                self.assertEqual(data['supplier_gstin'],expected)
                self.assertEqual(data['supplier_gstin_ocr_correction']['original'],raw)
                self.assertEqual(data['confidence']['supplier_gstin']['candidates'],[raw])
                self.assertEqual(data['confidence']['supplier_gstin']['level'],'low')
                self.assertIn(raw,data['extracted_text'])
                self.assertIn('Human review required',' '.join(data['validation_errors']))
                self.assertEqual(data['supplier_gstin_validation']['registration'],'not_verified')

    def test_letter_positions_and_entity_are_position_constrained(self):
        # Generate valid examples locally; this does not assert real registration.
        for body,index in [('08OBCDE1234F1Z',2),('08ABCDE1234O1Z',11),('08ABCDE1234FOZ',12)]:
            good=next(body+c for c in ALPHABET if gstin_evidence(body+c)['checksum_valid'])
            raw=good[:index]+'0'+good[index+1:]
            self.assertEqual(ocr_gstin_candidate(raw),good)

    def test_invalid_length_other_characters_or_checksum_are_not_invented(self):
        for raw in ('0O8ABCDE9100F1ZV','O8ABCDE1234F1Z9','O8ABCDEI234F1Z0','99ABCDE1234F1Z0','', 'O8AB'):
            self.assertIsNone(ocr_gstin_candidate(raw))
            data=self.parse(raw)
            self.assertNotIn('supplier_gstin_ocr_correction',data)

    def test_valid_identity_and_embedded_invalid_text_unchanged(self):
        self.assertIsNone(ocr_gstin_candidate('08ABCDE1234F1Z0'))
        data=self.parse('O8ABCDE1234F1Z0','embedded')
        self.assertEqual(data['supplier_gstin'],'O8ABCDE1234F1Z0')
        self.assertNotIn('supplier_gstin_ocr_correction',data)

    def test_conflicting_values_remain_unknown_and_pages_immutable(self):
        pages=[{'page':1,'method':'ocr','text':'Supplier GSTIN: O8ABCDE1234F1Z0\nSupplier GSTIN: O8ABCDE9100F1ZV','tables':[]}]
        before=deepcopy(pages);data=parse_document(pages,'image_ocr')
        self.assertIsNone(data['supplier_gstin']);self.assertEqual(pages,before)
        self.assertEqual(data['confidence']['supplier_gstin']['level'],'conflicting')

    def test_unknown_low_quality_remains_missing(self):
        data=parse_document([{'page':1,'method':'ocr','text':'unreadable image','tables':[]}],'image_ocr')
        self.assertIsNone(data['supplier_gstin']);self.assertIsNone(data['buyer_gstin'])
        self.assertEqual(data['confidence']['supplier_gstin']['level'],'missing')

    def test_candidate_warning_is_visible_in_shared_review_detail_notices(self):
        from django.template.loader import render_to_string
        data=self.parse('O8ABCDE1234F1Z0')
        html=render_to_string('invoices/extraction_current_notices.html',{'current_notices':data['validation_errors']})
        for text in ('O8ABCDE1234F1Z0','08ABCDE1234F1Z0','Human review required','not official registration verification'):
            self.assertIn(text,html)
