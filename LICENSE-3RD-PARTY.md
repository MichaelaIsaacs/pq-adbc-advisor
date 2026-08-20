# Third-party licenses

`pq-adbc-advisor` depends on the following third-party packages. Their
license texts are included by reference; installing the package via
`pip` fetches each dependency's full license from PyPI.

## Runtime dependencies

| Package | Version | License | Homepage |
|---|---|---|---|
| requests | >=2.28 | Apache-2.0 | https://requests.readthedocs.io/ |
| pandas | >=1.5 | BSD-3-Clause | https://pandas.pydata.org/ |

## Optional dependencies

| Package | Version | License | Homepage |
|---|---|---|---|
| sempy | >=0.4 | MIT | https://learn.microsoft.com/python/api/semantic-link |
| notebookutils | (Fabric-provided) | Microsoft | https://learn.microsoft.com/fabric/data-engineering/notebook-utilities |
| IPython | >=8.0 | BSD-3-Clause | https://ipython.org/ |

`sempy` and `notebookutils` are only used when running inside a Fabric
notebook; they are not installed by `pip install pq-adbc-advisor`.

## Dev dependencies

| Package | Version | License | Homepage |
|---|---|---|---|
| pytest | >=7.0 | MIT | https://pytest.org/ |

## Attribution

* Regex-based M language scanning is inspired by the reference grammar
  in the Power Query M formula language specification.
* The migration guidance rules in `troubleshoot.py` are derived from
  the internal Power Query CSS "ODBC-to-ADBC External Guide" (v3.0,
  2026-08-04). The public-facing version is at
  https://learn.microsoft.com/power-query/transition-to-adbc.
