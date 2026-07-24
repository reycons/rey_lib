# Workbook test fixtures

`synthetic.py` generates workbook fixtures for `test_workbook_conversion.py`
inside each test's temporary directory. They contain only synthetic values and
are not copied from installation or data-feed folders.

The focused tests generate XLSX-family fixtures with OpenPyXL. XLS and XLSB
dispatch behavior is covered with a neutral fake FastExcel reader because
OpenPyXL cannot write those formats. Real XLS/XLSB parsing capability was
verified separately against the upstream Calamine fixture suite during the
repository review.
