import os
from getpass import getpass
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

# --- Important: This setup must match your main app ---
from dashboard import app, db
from models import User

# Load environment variables to get the admin username
load_dotenv()
ADMIN_USER = os.getenv('ADMIN_USERNAME', 'admin')
# ---

def reset_password():
    """
    A command-line utility to forcefully reset the admin password.
    """
    with app.app_context():
        # Find the admin user
        admin = User.query.filter_by(username=ADMIN_USER).first()

        if not admin:
            print(f"Admin user '{ADMIN_USER}' not found. Let's create it.")
            admin = User(
                username=ADMIN_USER,
                email=f"{ADMIN_USER}@example.com",
                tradingview_user=f"tv_{ADMIN_USER}",
                is_superuser=True
            )
            db.session.add(admin)

        print(f"Resetting password for admin user: '{ADMIN_USER}'")
        
        # Securely prompt for the new password
        new_password = getpass("Enter the new password: ")
        confirm_password = getpass("Confirm the new password: ")

        if new_password != confirm_password:
            print("\nPasswords do not match. Aborting.")
            return

        if not new_password:
            print("\nPassword cannot be empty. Aborting.")
            return

        # Hash the new password and save it
        admin.password_hash = generate_password_hash(new_password)
        db.session.commit()

        print(f"\nPassword for '{ADMIN_USER}' has been successfully reset.")
        print("You can now start the main dashboard.py application and log in.")


if __name__ == '__main__':
    reset_password()