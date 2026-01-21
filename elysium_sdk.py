import json
import os
import requests
import time
import tarfile
import io
import shutil

class TrainingInput:
    def __init__(self, source, content_type=None, input_mode="File"):
        self.source = source
        self.content_type = content_type
        self.input_mode = input_mode

    def to_dict(self):
        return {
            "DataSource": {"Uri": self.source},
            "ContentType": self.content_type,
            "InputMode": self.input_mode
        }

class Estimator:
    def __init__(self, entry_point, role=None, instance_count=1, instance_type="local",
                 framework="pytorch", hyperparameters=None, output_path=None, image_uri=None):
        self.entry_point = entry_point
        self.instance_count = instance_count
        self.instance_type = instance_type
        self.framework = framework
        self.hyperparameters = hyperparameters or {}
        self.output_path = output_path
        self.image_uri = image_uri
        self.master_url = os.environ.get("ELYSIUM_MASTER_URL", "http://127.0.0.1:5000")

    def fit(self, inputs, source_dir=None, secrets=None, wait=True):
        """
        Submits the training job to the Elysium Master.
        inputs: dict of channel_name -> TrainingInput
        source_dir: Path to directory containing training code (optional)
        """
        # 1. Validate Inputs
        if not isinstance(inputs, dict):
            raise ValueError("inputs must be a dictionary of channel names to TrainingInput objects")
        if "train" not in inputs:
            raise ValueError("Must provide a 'train' channel in inputs")

        # 2. Package Source Code
        code_bytes = None
        if source_dir:
            if not os.path.isdir(source_dir): raise ValueError(f"source_dir {source_dir} not found")
            print(f"📦 Packaging source code from {source_dir}...")
            bio = io.BytesIO()
            with tarfile.open(fileobj=bio, mode='w:gz') as tar:
                tar.add(source_dir, arcname=os.path.basename(source_dir))
            code_bytes = bio.getvalue()
        elif os.path.isfile(self.entry_point):
            # If entry_point is a file path, package it
            print(f"📦 Packaging entry script {self.entry_point}...")
            bio = io.BytesIO()
            with tarfile.open(fileobj=bio, mode='w:gz') as tar:
                tar.add(self.entry_point, arcname=os.path.basename(self.entry_point))
            code_bytes = bio.getvalue()

        # 3. Serialize Job Spec
        input_data_config = {k: v.to_dict() for k, v in inputs.items()}

        job_spec = {
            "TrainingJobName": f"elysium-job-{int(time.time())}",
            "AlgorithmSpecification": {
                "TrainingImage": self.image_uri or "elysium-worker:latest",
                "TrainingInputMode": "File",
                "Framework": self.framework,
                "ContainerEntrypoint": [os.path.basename(self.entry_point)]
            },
            "HyperParameters": self.hyperparameters,
            "InputDataConfig": input_data_config,
            "OutputDataConfig": {
                "OutputPath": self.output_path or "/tmp/output"
            },
            "ResourceConfig": {
                "InstanceType": self.instance_type,
                "InstanceCount": self.instance_count,
                "VolumeSizeInGB": 30
            }
        }

        # Add Secrets if provided
        if secrets:
            job_spec["Secrets"] = secrets

        print(f"🚀 Submitting Job: {job_spec['TrainingJobName']}...")

        try:
            files = {}
            if code_bytes:
                files = {'code': ('source.tar.gz', code_bytes, 'application/gzip')}

            # Send as Multipart
            r = requests.post(
                f"{self.master_url}/api/job/start",
                data={'spec': json.dumps(job_spec)},
                files=files,
                timeout=30
            )

            if r.status_code == 200:
                print("✅ Job Submitted Successfully.")
                if wait:
                    self._wait_for_job(job_spec['TrainingJobName'])
            else:
                print(f"❌ Submission Failed: {r.text}")
        except Exception as e:
            print(f"❌ Connection Error: {e}")

    def _wait_for_job(self, job_name):
        print(f"⏳ Waiting for {job_name} to complete...")
        # Polling logic would go here
        pass
