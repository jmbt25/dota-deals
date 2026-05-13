"""JSON publish layer.

Reads from the SQLite DB, builds typed wire-format Pydantic models, and
writes deterministic JSON files into ``public/data/`` for Cloudflare
Pages to serve. Also exposes the R2 client used by the scheduled GitHub
Actions workflow to sync the DB between runs.

Wire format conventions (also in docs/PUBLISH.md):

* **Keys** are ``snake_case`` end-to-end. The frontend camelizes at the
  fetch boundary; the pipeline doesn't carry an aliasing layer.
* **Datetimes** are ISO 8601 with a ``Z`` suffix; date-only values are
  ``YYYY-MM-DD``.
* **Nulls** are included (``"field": null``) rather than omitted. Stable
  contract over smaller payloads.
* **Buy scores** are plain floats at native Python precision. Display
  rounding is the frontend's job.
* **Prices** are USD strings formatted from integer cents (``100099`` →
  ``"1000.99"``). Avoids float-display surprises in JS.
"""
