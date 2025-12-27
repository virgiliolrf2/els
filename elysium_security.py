import base64
import json
import os
import secrets
from pathlib import Path
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding

def generate_master_keys(private_path="master_payment_private.pem", public_path="master_payment_public.pem"):
    """
    Generates a 4096-bit RSA key pair for the Master Node.
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=4096
    )

    # Save Private
    with open(private_path, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ))

    # Save Public
    public_key = private_key.public_key()
    with open(public_path, "wb") as f:
        f.write(public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ))

    return True

def generate_secure_wallet_id(payment_method, payment_address, master_public_key_pem):
    """
    Worker Side: Encrypts payment details into a Wallet ID.
    """
    payload = {
        "type": payment_method,
        "account": payment_address,
        "nonce": secrets.token_hex(16) # Randomness to ensure unique IDs for same address
    }

    payload_bytes = json.dumps(payload).encode('utf-8')

    public_key = serialization.load_pem_public_key(master_public_key_pem)

    ciphertext = public_key.encrypt(
        payload_bytes,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None
        )
    )

    b64_str = base64.urlsafe_b64encode(ciphertext).decode('utf-8')
    return f"ELYS-SECure-{b64_str}"

def decrypt_payment_info(secure_wallet_id, master_private_key_path):
    """
    Master Side: Decrypts the Wallet ID to reveal payment info.
    """
    if not secure_wallet_id.startswith("ELYS-SECure-"):
        raise ValueError("Invalid Wallet ID Format")

    b64_str = secure_wallet_id.replace("ELYS-SECure-", "")
    ciphertext = base64.urlsafe_b64decode(b64_str)

    with open(master_private_key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(
            f.read(),
            password=None
        )

    plaintext = private_key.decrypt(
        ciphertext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None
        )
    )

    return json.loads(plaintext.decode('utf-8'))
