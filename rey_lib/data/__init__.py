"""The shared data contracts, owned by neither endpoint family.

A load is one operation between two data objects:

    DataObject -> TransformObject -> DataObject

``source`` and ``target`` are ROLES in that operation and not classes, so
there is deliberately **no DataObject base or protocol here**. A type is
introduced when behaviour needs one; naming a directory is not behaviour.

What lives here is what both families and the operation between them share:

    DataTransform / IdentityTransform   what the produced records contain
    DataProfile / ProfileField          the shape and character of data
    DataStructureError                  a data object's structure being wrong
    TransformError                      one column's rule failing to apply

The families themselves live beside this, not under it:

    rey_lib/files/   file-backed data objects
    rey_lib/db/      database-backed data objects
    rey_lib/load/    the operation that moves one into another

**Nothing here imports either family.** This is the bottom of that stack, and
a contract that reached upward into one implementation family would make that
family privileged -- which is exactly what putting the load under ``files/``
did.
"""

from __future__ import annotations

from rey_lib.data.data_profile import (
    DataProfile,
    FieldProfile,
    ProfileField,
    profile_for,
)
from rey_lib.data.column_transform import (
    OUTPUT_DATATYPES,
    ColumnTransform,
    authorable_starters,
    is_exported,
)
from rey_lib.data.data_transform import (
    IDENTITY_EXECUTION,
    DataTransform,
    DeclaredTransform,
    ExecutionForm,
    IdentityTransform,
)
from rey_lib.data.errors import DataStructureError, TransformError

__all__ = [
    "IDENTITY_EXECUTION",
    "OUTPUT_DATATYPES",
    "DataProfile",
    "DataStructureError",
    "TransformError",
    "ColumnTransform",
    "DataTransform",
    "DeclaredTransform",
    "ExecutionForm",
    "FieldProfile",
    "IdentityTransform",
    "ProfileField",
    "authorable_starters",
    "is_exported",
    "profile_for",
]
