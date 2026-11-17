"""Scheme-specific checks. A module here is run only for schemes whose checker/schemes/<id>.json
lists it under "extra_checks". It sets KIND (the case kind), NEEDS (record field names; "photo.<field>"
for photo fields, which also means it gets only records whose photos were fetched) and
find(scheme, records, cfg) returning cases built with checker.common.make_case."""
