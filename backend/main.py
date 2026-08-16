import os
import uuid
import mimetypes
from datetime import datetime

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

BUCKET_NAME = os.environ.get("DROPFORGE_BUCKET", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_PROFILE = os.environ.get("AWS_PROFILE", "")
URL_EXPIRY_SECONDS = int(os.environ.get("DROPFORGE_URL_EXPIRY", "300"))
MAX_FILE_SIZE_BYTES = int(os.environ.get("DROPFORGE_MAX_BYTES", str(5 * 1024 * 1024)))

ALLOWED_CONTENT_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "application/pdf", "text/plain", "text/csv",
    "application/zip", "video/mp4",
}

app = FastAPI(title="DropForge")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("DROPFORGE_ALLOWED_ORIGINS", "*").split(","),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

session = boto3.Session(profile_name=AWS_PROFILE) if AWS_PROFILE else boto3.Session()
s3_client = session.client("s3", region_name=AWS_REGION, config=Config(signature_version="s3v4"))


class UploadRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    content_type: str


class UploadResponse(BaseModel):
    upload_url: str
    object_key: str
    expires_in: int
    public_url: str


@app.get("/health")
def health():
    return {"status": "ok", "bucket": BUCKET_NAME, "region": AWS_REGION}


@app.post("/upload-url", response_model=UploadResponse)
def create_upload_url(req: UploadRequest):
    if not BUCKET_NAME:
        raise HTTPException(500, "DROPFORGE_BUCKET is not configured")
    if req.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(400, f"Content type '{req.content_type}' is not allowed")

    safe_name = "".join(c for c in req.filename if c.isalnum() or c in "._-") or "file"
    date_prefix = datetime.utcnow().strftime("%Y/%m/%d")
    object_key = f"uploads/{date_prefix}/{uuid.uuid4().hex}-{safe_name}"

    try:
        upload_url = s3_client.generate_presigned_url(
            ClientMethod="put_object",
            Params={"Bucket": BUCKET_NAME, "Key": object_key, "ContentType": req.content_type},
            ExpiresIn=URL_EXPIRY_SECONDS,
        )
    except ClientError as e:
        raise HTTPException(500, f"Could not generate upload URL: {e}")

    public_url = f"https://{BUCKET_NAME}.s3.{AWS_REGION}.amazonaws.com/{object_key}"
    return UploadResponse(upload_url=upload_url, object_key=object_key,
                           expires_in=URL_EXPIRY_SECONDS, public_url=public_url)


class FileInfo(BaseModel):
    key: str
    size_bytes: int
    last_modified: str
    url: str


class FileListResponse(BaseModel):
    files: list[FileInfo]
    count: int


@app.get("/files", response_model=FileListResponse)
def list_files(prefix: str = Query("uploads/", description="Key prefix to filter by")):
    if not BUCKET_NAME:
        raise HTTPException(500, "DROPFORGE_BUCKET is not configured")

    try:
        response = s3_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=prefix)
    except ClientError as e:
        raise HTTPException(500, f"Could not list files: {e}")

    files = []
    for obj in response.get("Contents", []):
        files.append(FileInfo(
            key=obj["Key"],
            size_bytes=obj["Size"],
            last_modified=obj["LastModified"].isoformat(),
            url=f"https://{BUCKET_NAME}.s3.{AWS_REGION}.amazonaws.com/{obj['Key']}",
        ))

    return FileListResponse(files=files, count=len(files))


@app.delete("/files/{object_key:path}")
def delete_file(object_key: str):
    if not BUCKET_NAME:
        raise HTTPException(500, "DROPFORGE_BUCKET is not configured")

    try:
        s3_client.delete_object(Bucket=BUCKET_NAME, Key=object_key)
    except ClientError as e:
        raise HTTPException(500, f"Could not delete file: {e}")

    return {"status": "deleted", "key": object_key}


class UploadPostResponse(BaseModel):
    url: str
    fields: dict
    object_key: str
    max_size_bytes: int


@app.post("/upload-url-post", response_model=UploadPostResponse)
def create_upload_post(req: UploadRequest):
    if not BUCKET_NAME:
        raise HTTPException(500, "DROPFORGE_BUCKET is not configured")
    if req.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(400, f"Content type '{req.content_type}' is not allowed")

    safe_name = "".join(c for c in req.filename if c.isalnum() or c in "._-") or "file"
    date_prefix = datetime.utcnow().strftime("%Y/%m/%d")
    object_key = f"uploads/{date_prefix}/{uuid.uuid4().hex}-{safe_name}"

    try:
        presigned = s3_client.generate_presigned_post(
            Bucket=BUCKET_NAME,
            Key=object_key,
            Fields={"Content-Type": req.content_type},
            Conditions=[
                {"Content-Type": req.content_type},
                ["content-length-range", 1, MAX_FILE_SIZE_BYTES],
            ],
            ExpiresIn=URL_EXPIRY_SECONDS,
        )
    except ClientError as e:
        raise HTTPException(500, f"Could not generate upload POST: {e}")

    return UploadPostResponse(
        url=presigned["url"],
        fields=presigned["fields"],
        object_key=object_key,
        max_size_bytes=MAX_FILE_SIZE_BYTES,
    )
