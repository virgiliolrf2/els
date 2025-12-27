import json
import os
import requests
import time
import tarfile
import io

class TrainingInput:
    def __init__(self, source, content_type=None, input_mode="File"):
        self.source = source
        self.content_type = content_type
        self.input_mode = input_mode

    def to_dict(self):
        return {
            "DataSource": {
                "S3DataSource": {
                    "S3Uri": self.source,
                    "S3DataType": "S3Prefix",
                    "S3DataDistributionType": "FullyReplicated"
                }
            },
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

    def fit(self, inputs, wait=True):
        """
        Submits the training job to the Elysium Master.
        inputs: dict of channel_name -> TrainingInput
        """
        # 1. Validate Inputs
        if not isinstance(inputs, dict):
            raise ValueError("inputs must be a dictionary of channel names to TrainingInput objects")
        if "train" not in inputs:
            raise ValueError("Must provide a 'train' channel in inputs")

        # 2. Serialize Job Spec
        input_data_config = {k: v.to_dict() for k, v in inputs.items()}

        job_spec = {
            "TrainingJobName": f"elysium-job-{int(time.time())}",
            "AlgorithmSpecification": {
                "TrainingImage": self.image_uri or "elysium-worker:latest",
                "TrainingInputMode": "File",
                "Framework": self.framework,
                "ContainerEntrypoint": [self.entry_point]
            },
            "HyperParameters": self.hyperparameters,
            "InputDataConfig": input_data_config,
            "OutputDataConfig": {
                "S3OutputPath": self.output_path or "s3://elysium-artifacts/output"
            },
            "ResourceConfig": {
                "InstanceType": self.instance_type,
                "InstanceCount": self.instance_count,
                "VolumeSizeInGB": 30
            },
            # In a real SDK, we would upload the source code (entry_point) to S3 here
            # and pass the S3 URI as 'SourceCode' in the spec.
            # For MVP, we assume entry_point is available or passed as content.
            "SourceCode": self.entry_point # Simplified for prototype
        }

        print(f"🚀 Submitting Job: {job_spec['TrainingJobName']}...")

        try:
            r = requests.post(f"{self.master_url}/api/job/start", json=job_spec, timeout=10)
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
