def spreadsheet_value(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def spreadsheet_row(values):
    return [spreadsheet_value(value) for value in values]


def sanitize_workbook(workbook):
    for sheet in workbook.worksheets:
        for row in sheet:
            for cell in row:
                if isinstance(cell.value, str):
                    cell.value = spreadsheet_value(cell.value)
