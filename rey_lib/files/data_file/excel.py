"""Excel workbooks: what an inventoried workbook IS.

One family over the workbook formats the estate accepts -- XLS, XLSX, XLSB and
XLSM, the set ``workbook_conversion.SUPPORTED_WORKBOOK_EXTENSIONS`` names. A
workbook is a valid DataFile from the moment it is governed; turning it into
CSV is a TRANSFORM's business, and nothing here converts.

**Read through FastExcel**, the one declared library that opens all four.
pandas' default engines read only XLSX and XLSM in this estate (XLS needs xlrd
and XLSB pyxlsb, neither declared), so a reader built on them could not serve
the family.

**The first worksheet, header on its first row**, which is what the loader's
workbook reader read before this format had a DataFile. Every cell is read as
text and an empty cell is ``""``, the same answer a delimited file gives for an
empty field -- so a workbook and a CSV holding the same table read alike.

The header is the declaration, so this validates BEFORE reading, the cheap order
a delimited file uses.
"""

from __future__ import annotations

from typing import Any

from rey_lib.data.errors import DataStructureError
from rey_lib.files.data_file import data_file
from rey_lib.files.data_file.base import DataFile
from rey_lib.logs import get_logger

__all__ = ["ExcelDataFile", "XlsFile", "XlsbFile", "XlsmFile", "XlsxFile"]

_logger = get_logger(__name__)

#: The worksheet this format reads: the first, as the loader's reader did.
_FIRST_SHEET = 0


class ExcelDataFile(DataFile):
    """A workbook whose first worksheet's first row names its columns.

    Everything is shared across the family; a subtype supplies only the token
    it is registered under.
    """

    @property
    def declares_structure(self) -> bool:
        """The header row is the declaration: every column, in order, before
        any row is read."""
        return True

    def source_structure(self) -> list[str]:
        """The header row's column names, in order, without reading the rows.

        Returns:
            The declared columns, or an empty list for an empty worksheet.

        Raises:
            DataStructureError: If the workbook cannot be opened or read.
        """
        sheet = self._load(n_rows=0)
        return [column.name for column in sheet.available_columns()]

    def read(self) -> list[dict[str, Any]]:
        """Every row of the first worksheet, as text, keyed by the header."""
        return self._records(self._load(dtypes="string"))

    def sample(self, limit: int) -> list[dict[str, Any]]:
        """Up to ``limit`` rows, reading no more of the worksheet than that.

        The override the base class invites: FastExcel can stop early, so
        this does rather than reading everything and slicing.
        """
        if limit <= 0:
            return []
        return self._records(self._load(n_rows=limit, dtypes="string"))

    def validate(self, expected_columns: list[str] | None = None) -> None:
        """Check the header row, against the destination or against itself.

        The rule a delimited file's header follows, because a header row is
        the same artifact: ORDERED against a destination, and merely present
        without one.
        """
        if expected_columns is None:
            if not self.source_structure():
                raise DataStructureError(
                    f"'{self.path.name}' has no header row, so its columns "
                    "cannot be established."
                )
            return

        try:
            actual = self.source_structure()
        except DataStructureError as exc:
            raise DataStructureError(
                str(exc), validation_name="load_header"
            ) from exc

        if actual == expected_columns:
            return

        _logger.error(
            "Load header validation failed for '%s'\n"
            "Expected table columns:\n%s\n\n"
            "Actual file columns:\n%s",
            self.path.name,
            ",".join(expected_columns),
            ",".join(actual),
        )
        raise DataStructureError(
            "Header mismatch", validation_name="load_header"
        )

    def read_validated(
        self,
        expected_columns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Validate the header row, THEN read -- the cheap order for this
        format."""
        self.validate(expected_columns)
        return self.read()

    def _load(self, **options: Any) -> Any:
        """Open the workbook and load its first worksheet.

        Args:
            options: Passed to FastExcel's ``load_sheet`` -- ``n_rows``,
                ``dtypes``.

        Returns:
            The loaded FastExcel worksheet.

        Raises:
            DataStructureError: If the workbook cannot be opened or the
                worksheet read. FastExcel names what went wrong; this names
                the file.
        """
        import fastexcel  # noqa: PLC0415 -- the rey_lib 'files' extra

        _logger.debug("Reading worksheet %d of %r", _FIRST_SHEET, self)
        try:
            return fastexcel.read_excel(self.path).load_sheet(
                _FIRST_SHEET, **options
            )
        except fastexcel.FastExcelError as exc:
            raise DataStructureError(
                f"Cannot read '{self.path.name}': {exc}"
            ) from exc

    @staticmethod
    def _records(sheet: Any) -> list[dict[str, Any]]:
        """The worksheet's rows, with an empty cell read as ``""``."""
        return [
            {name: "" if value is None else value for name, value in row.items()}
            for row in sheet.to_polars().to_dicts()
        ]


@data_file("XLS")
class XlsFile(ExcelDataFile):
    """A legacy binary Excel workbook."""

    @property
    def file_type(self) -> str:
        """XLS."""
        return "XLS"


@data_file("XLSX")
class XlsxFile(ExcelDataFile):
    """An Office Open XML Excel workbook."""

    @property
    def file_type(self) -> str:
        """XLSX."""
        return "XLSX"


@data_file("XLSB")
class XlsbFile(ExcelDataFile):
    """A binary Excel workbook."""

    @property
    def file_type(self) -> str:
        """XLSB."""
        return "XLSB"


@data_file("XLSM")
class XlsmFile(ExcelDataFile):
    """A macro-enabled Office Open XML Excel workbook."""

    @property
    def file_type(self) -> str:
        """XLSM."""
        return "XLSM"
