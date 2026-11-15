import r2_common as common

SOURCE_KEY = "snapshots/20261024_222129/public/financial_reports.parquet"
TARGET_KEY = "archive/financial_reports_full_20261024.parquet"

common.setup_logging("archive_copy")
common.load_env()
s3, bucket = common.get_r2()

s3.copy_object(
    Bucket=bucket,
    CopySource={"Bucket": bucket, "Key": SOURCE_KEY},
    Key=TARGET_KEY,
)

source_size = common.remote_size(s3, bucket, SOURCE_KEY)
target_size = common.remote_size(s3, bucket, TARGET_KEY)
status = "OK" if source_size == target_size else "UKURAN TIDAK COCOK"
print(f"sumber={source_size} salinan={target_size} status={status}")
