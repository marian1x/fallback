# utils.py
import os
from cryptography.fernet import Fernet
from dotenv import load_dotenv

ENV_PATH = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(ENV_PATH)

# Load the encryption key from .env or generate a new one
ENCRYPTION_KEY_STR = os.getenv("ENCRYPTION_KEY")
if not ENCRYPTION_KEY_STR:
    ENCRYPTION_KEY = Fernet.generate_key()
    # IMPORTANT: Save this key to your .env file!
    with open('.env', 'a') as f:
        f.write(f'\nENCRYPTION_KEY={ENCRYPTION_KEY.decode("utf-8")}')
else:
    ENCRYPTION_KEY = ENCRYPTION_KEY_STR.encode('utf-8')

cipher_suite = Fernet(ENCRYPTION_KEY)

def encrypt_data(data: str) -> str:
    """Encrypts a string and returns it as a string."""
    if not data:
        return ""
    encrypted_bytes = cipher_suite.encrypt(data.encode('utf-8'))
    return encrypted_bytes.decode('utf-8')

def decrypt_data(encrypted_data: str) -> str:
    """Decrypts a string and returns it as a string."""
    if not encrypted_data:
        return ""
    try:
        decrypted_bytes = cipher_suite.decrypt(encrypted_data.encode('utf-8'))
        return decrypted_bytes.decode('utf-8')
    except Exception:
        # Return empty if decryption fails (e.g., invalid token)
        return ""
