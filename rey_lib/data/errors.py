"""What a data object's structure failing looks like.

One error, in the layer both families of data object depend on. It was
``DataFileStructureError`` under ``files/data_file/``, and the name was a
statement the model had outgrown: a delimited file, a keyed file and a
database query are all data objects whose STRUCTURE can be wrong, and only one
of those families is a file.

It moved for a second reason as well, and that one is structural rather than a
matter of naming. ``DataTransform`` raises it, and ``DataTransform`` belongs
here -- so leaving the error under ``files/`` would have made this layer
import back upward into one implementation family to describe a failure that
is neither family's.
"""

from __future__ import annotations

from rey_lib.errors.error_utils import AppError

__all__ = ["DataStructureError"]


class DataStructureError(AppError):
    """A data object whose structure is not what it must be.

    Distinct from a database error on purpose: a source that does not match
    its destination is a SOURCE fault, and reporting it as a failed insert --
    which is what happens when the check is missing -- names the wrong thing.

    Carries ``validation_name``, which is what the RUN LOG records. The
    subtype supplies it because only the subtype knows which check it ran; a
    caller deriving it would be branching on format to write a log line.
    """

    def __init__(
        self,
        message: str,
        *,
        validation_name: str = "data_file_structure",
    ) -> None:
        """Hold the failure and the name the run log knows it by.

        **THE DEFAULT KEEPS ITS OLD SPELLING, deliberately.** It is not a
        class name -- it is the value written to the run log by the three
        sites that do not name their own check, so changing it would rewrite
        recorded evidence to tidy a word. The same reasoning
        ``DelimitedHeaderFile.validate`` already states when it preserves
        ``load_header`` for a failure whose message it rewrote.

        Args:
            message: What was wrong, as the run log will record it.
            validation_name: Which check this was, named by whatever ran it.
        """
        super().__init__(message)
        self.validation_name = validation_name
