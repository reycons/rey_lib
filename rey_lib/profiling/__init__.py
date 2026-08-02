"""File structure profiling utilities."""

from rey_lib.profiling.csv_profile import (
    enrich_csv_profile,
    normalized_header,
    same_header_errors,
)
from rey_lib.profiling.file_profiler import infer_col_type, infer_sql_type, profile_rows
from rey_lib.profiling.profile_validation import validate_csv_profile
from rey_lib.profiling.structural_analysis import (
    ANALYSIS_LIMITS,
    AnalysisLimits,
    StructuralAnalysis,
    build_structural_analysis,
    field_characteristic,
    length_bucket,
    structural_descriptor,
)

__all__ = [
    "profile_rows",
    "infer_col_type",
    "infer_sql_type",
    "enrich_csv_profile",
    "normalized_header",
    "same_header_errors",
    "validate_csv_profile",
    "ANALYSIS_LIMITS",
    "AnalysisLimits",
    "StructuralAnalysis",
    "build_structural_analysis",
    "field_characteristic",
    "length_bucket",
    "structural_descriptor",
]
