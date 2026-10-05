# s3_seed

What the run uploads to `test-bucket`, as httpfs CI does in `scripts/minio_s3.yml`, for the
presigned-URL and attach tests. The path under `s3_seed/` is the key in the bucket.

Here:
- `presigned/phonenumbers.csv`: a copy of DuckDB's `data/csv/phonenumbers.csv`.
- `presigned/t1.parquet`: a copy of DuckDB's `data/parquet-testing/glob/t1.parquet`.

Not here, because they are generated and large: run `scripts/generate_presigned_url.sh` in the httpfs
checkout, then copy from its `test/test_data/`:
- `presigned-url-lineitem.parquet` to `presigned/lineitem_large.parquet`
- `attach.db` to `presigned/attach.db`
- `lineitem_sf1.db` to `presigned/lineitem_sf1.db`
