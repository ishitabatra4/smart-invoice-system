(function () {
  function addLine(doc) {
    const count = doc.getElementById('id_items-TOTAL_FORMS');
    const index = Number(count.value);
    if (!Number.isInteger(index) || index < 0 || index >= 100) return false;
    const html = doc.getElementById('empty-adjustment-line').innerHTML.replaceAll('__prefix__', String(index));
    doc.getElementById('adjustment-lines').insertAdjacentHTML('beforeend', html);
    count.value = String(index + 1);
    return true;
  }
  if (typeof module !== 'undefined') module.exports = {addLine};
  if (typeof document !== 'undefined') document.getElementById('add-adjustment-line').addEventListener('click', () => addLine(document));
})();
