import os
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
from cryptography.exceptions import InvalidSignature

def generate_key():
    """Generates a new ECDSA Private Key (SECP256K1)."""
    return ec.generate_private_key(ec.SECP256K1())

def save_key(private_key, path):
    """Saves private key to a PEM file."""
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    with open(path, 'wb') as f:
        f.write(pem)

def load_key(path):
    """Loads private key from a PEM file."""
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as f:
        return serialization.load_pem_private_key(f.read(), password=None)

def get_public_key_pem(private_key):
    """Returns the Public Key in PEM format."""
    public_key = private_key.public_key()
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode('utf-8')

def sign_message(private_key, message):
    """Signs a string message. Returns hex signature."""
    if isinstance(message, str):
        message = message.encode('utf-8')
    signature = private_key.sign(
        message,
        ec.ECDSA(hashes.SHA256())
    )
    return signature.hex()

def verify_signature(public_key_pem, message, signature_hex):
    """Verifies a signature. Returns True if valid."""
    try:
        if isinstance(public_key_pem, str):
            public_key_pem = public_key_pem.encode('utf-8')

        public_key = serialization.load_pem_public_key(public_key_pem)

        if isinstance(message, str):
            message = message.encode('utf-8')

        signature = bytes.fromhex(signature_hex)

        public_key.verify(
            signature,
            message,
            ec.ECDSA(hashes.SHA256())
        )
        return True
    except (InvalidSignature, ValueError, Exception) as e:
        # print(f"Verification Failed: {e}")
        return False
