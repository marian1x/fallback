from cryptography.fernet import Fernet

# Generate a new key
key = Fernet.generate_key()

# Print the key. It will be in bytes, like b'...'
print("Your new ENCRYPTION_KEY is:")
print(key)

# For easy copy-pasting into the .env file, you can also print it as a string
print("\nCopy this line into your .env file:")
print(f"ENCRYPTION_KEY={key.decode()}")