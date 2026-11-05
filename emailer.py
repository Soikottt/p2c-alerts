import smtplib
from email.mime.text import MIMEText
import config

def send_email(to_email, subject, body, cc_emails=None):
    """Sends an email via Gmail SMTP."""
    if cc_emails is None:
        cc_emails = []
        
    # In our testing, we will just print to the screen if DRY_RUN is true
    if config.DRY_RUN:
        print("\n--- DRY RUN: EMAIL NOT ACTUALLY SENT ---")
        print(f"To: {to_email}")
        if cc_emails:
            print(f"CC: {', '.join(cc_emails)}")
        print(f"Subject: {subject}")
        print(f"Body:\n{body}")
        print("----------------------------------------\n")
        return

    # Create the email message
    msg = MIMEText(body)
    msg["From"] = config.SMTP_USER
    msg["To"] = to_email
    if cc_emails:
        msg["Cc"] = ", ".join(cc_emails)
    msg["Subject"] = subject

    # Connect to Gmail and send
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
        server.starttls() # Secure the connection
        server.login(config.SMTP_USER, config.SMTP_PASS)
        
        # Combine To and CC into one list for sending
        recipients = [to_email] + cc_emails
        server.sendmail(config.SMTP_USER, recipients, msg.as_string())
        print(f"✅ Email sent successfully to {to_email}")

# --- THIS IS THE TEST BLOCK ---
if __name__ == "__main__":
    print("Testing Emailer...")
    
    # We will temporarily force DRY_RUN to False just for this test
    # so you can see a real email in your inbox.
    config.DRY_RUN = False 
    
    test_to = config.SMTP_USER # Send it to yourself
    test_cc = []
    
    send_email(
        to_email=test_to,
        subject="Test Alert: Pay Per Call Tool",
        body="This is a test email.\n\nIf you are reading this, Step 4 is complete and your tool can send emails!",
        cc_emails=test_cc
    )