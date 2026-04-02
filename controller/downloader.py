import os
import requests
import boto3
from urllib.parse import urlparse
import uuid
import shutil

class UniversalDownloader:
    def __init__(self, base_dir="/app/tmp"):
        self.base_dir = base_dir

    def create_workspace(self, task_id:str):
        workspace = os.path.join(self.base_dir, task_id)
        os.makedirs(workspace, exist_ok=True)
        return workspace
    
    def download(self, input_path: str, task_id: str):
        workspace = self.create_workspace(task_id=task_id)

        parsed = urlparse(input_path)

        filename = os.path.basename(parsed.path)
        dest_path = os.path.join(workspace, filename)

        if parsed.scheme == 's3':
            return self._download_from_s3(parsed.netloc, parsed.path.lstrip('/'), dest_path)
        
        elif parsed.scheme in ['http', 'https']:
            return self._download_from_url(input_path, dest_path)
        
        elif os.path.exists(input_path):
            return self._copy_from_local(input_path, dest_path)
        
        else:
            raise ValueError(f"Not support for unknown path: {input_path}")

    def _copy_from_local(self, src, dest):
        print(f"Copy local file {src} -> {dest}")
        shutil.copy2(src, dest)
        return dest 

    def _download_from_s3(self, bucket, key, dest):
        print(f"Download from S3")
        s3 = boto3.client('s3')
        s3.download_file(bucket, key, dest)
        return dest
    
    def _download_from_url(self, url, dest):
        print(f"Download from URL")
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            with open(dest, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

        return dest
        