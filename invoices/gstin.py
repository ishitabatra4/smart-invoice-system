"""Local syntax/check-digit evidence only; never an official registration lookup."""
import re
from django.core.exceptions import ValidationError
ALPHABET='0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
STATES={f'{n:02}' for n in (*range(1,39),97)}


def gstin_evidence(value):
    normalized=re.sub(r'\s+','',value or '').upper()
    structural=bool(re.fullmatch(r'[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[A-Z0-9]',normalized)) and normalized[:2] in STATES
    checksum=False
    if structural:
        total=0
        for index,char in enumerate(reversed(normalized[:-1])):
            product=ALPHABET.index(char)*(2 if index%2==0 else 1);total+=product//36+product%36
        checksum=normalized[-1]==ALPHABET[(36-total%36)%36]
    return {'value':normalized,'structural_valid':structural,'checksum_valid':checksum,'registration':'not_verified'}


def ocr_gstin_candidate(value):
    """Suggest only a unique 0/O repair; never repair length or other characters."""
    original=gstin_evidence(value)['value']
    if len(original)!=15 or gstin_evidence(original)['checksum_valid']:
        return None
    chars=list(original)
    for index,char in enumerate(chars[:14]):
        if index in (0,1,7,8,9,10) and char=='O':chars[index]='0'
        elif index in (2,3,4,5,6,11,12) and char=='0':chars[index]='O'
    # The check digit is alphanumeric, so position alone cannot distinguish 0/O.
    endings={chars[14]}
    if chars[14] in '0O':endings.update('0O')
    candidates={''.join(chars[:14])+ending for ending in endings}
    valid=[candidate for candidate in candidates if candidate!=original and gstin_evidence(candidate)['checksum_valid']]
    return valid[0] if len(valid)==1 else None


# Local registration-state consistency, not a determination of place of supply.
STATE_NAMES = dict(zip((f'{n:02}' for n in range(1,39)), (
    'Jammu and Kashmir','Himachal Pradesh','Punjab','Chandigarh','Uttarakhand','Haryana','Delhi','Rajasthan',
    'Uttar Pradesh','Bihar','Sikkim','Arunachal Pradesh','Nagaland','Manipur','Mizoram','Tripura','Meghalaya',
    'Assam','West Bengal','Jharkhand','Odisha','Chhattisgarh','Madhya Pradesh','Gujarat','Daman and Diu',
    'Dadra and Nagar Haveli and Daman and Diu','Maharashtra','Andhra Pradesh (old)','Karnataka','Goa',
    'Lakshadweep','Kerala','Tamil Nadu','Puducherry','Andaman and Nicobar Islands','Telangana','Andhra Pradesh','Ladakh')))
STATE_NAMES['97']='Other Territory'


def validate_gstin(value,label):
    if value and not gstin_evidence(value)['checksum_valid']:
        raise ValidationError(f'{label} fails local structure/state/checksum validation. Review the registered identity with the owner; do not substitute a sample identifier. Existing records were not changed.')


def validate_registration_state(gstin,state,label):
    validate_gstin(gstin,label+' GSTIN')
    if not gstin or not (state or '').strip():return
    normalize=lambda s:re.sub(r'[^a-z0-9]','',s.lower().replace('&','and'))
    codes={normalize(name):code for code,name in STATE_NAMES.items()}
    codes.update({'orissa':'21','uttaranchal':'05','pondicherry':'34','dadraandnagarhaveli':'26','nctofdelhi':'07','newdelhi':'07'})
    text=state.strip();code=text if text in STATES else codes.get(normalize(text))
    if code is None:
        raise ValidationError(f'{label} state could not be checked. Review its state name or two-digit code with the owner before creating this document.')
    if code!=gstin_evidence(gstin)['value'][:2]:
        raise ValidationError(f'{label} state does not match its GSTIN registration state. Review the identity with the owner; no stored identity was changed.')


def validate_document_gstins(supplier_gstin,buyer_gstin,supplier_state_code,tax_amount):
    validate_gstin(supplier_gstin,'Supplier GSTIN')
    validate_gstin(buyer_gstin,'Buyer GSTIN')
    if tax_amount>0 and not supplier_gstin:
        raise ValidationError('A GST-bearing invoice requires a locally validated supplier GSTIN. Review unregistered or special-supply cases independently; do not invent a GSTIN or remove tax to bypass validation. Genuine zero-tax/non-GST invoices remain supported.')
    if supplier_gstin and supplier_state_code!=gstin_evidence(supplier_gstin)['value'][:2]:
        raise ValidationError('Supplier state must match the supplier GSTIN registration state.')
