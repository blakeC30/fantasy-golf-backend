"""
Email service — sends transactional emails via AWS SES.

In local development, AWS_ENDPOINT_URL points to LocalStack, which accepts
SES API calls but does not actually deliver emails. The reset URL is always
logged to the console so developers can copy it without checking their inbox.

In production, boto3 uses the EC2 IAM instance role for credentials, so
AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY should be left empty in the prod
environment. The only required production config is SES_FROM_EMAIL (must be
verified in the AWS SES console) and AWS_REGION.
"""

import logging

import boto3

from app.config import settings

log = logging.getLogger(__name__)


def _ses_client():
    """Build a boto3 SES client, optionally pointed at LocalStack."""
    kwargs: dict = {"region_name": settings.AWS_REGION}
    if settings.AWS_ENDPOINT_URL:
        kwargs["endpoint_url"] = settings.AWS_ENDPOINT_URL
    if settings.AWS_ACCESS_KEY_ID:
        kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
    if settings.AWS_SECRET_ACCESS_KEY:
        kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
    return boto3.client("ses", **kwargs)


def send_password_reset_email(to_email: str, raw_token: str) -> None:
    """
    Send a password reset email to the given address.

    The raw token (not its hash) is embedded in the reset URL. The link is
    also logged at INFO level so it's easy to test locally without real email.
    """
    reset_url = f"{settings.FRONTEND_URL}/reset-password?token={raw_token}"

    # Always log the URL — essential for local dev where no email is delivered.
    log.info("Password reset URL for %s: %s", to_email, reset_url)

    text_body = (
        "You requested a password reset for your League Caddie account.\n\n"
        f"Click the link below to set a new password (expires in {settings.RESET_TOKEN_EXPIRE_HOURS} hour(s)):\n\n"
        f"{reset_url}\n\n"
        "If you didn't request this, you can safely ignore this email.\n"
    )

    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Reset your League Caddie password</title>
</head>
<body style="margin:0;padding:0;background-color:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f3f4f6;padding:40px 16px;">
    <tr>
      <td align="center">
        <table width="100%" style="max-width:520px;background-color:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

          <!-- Header — matches Login/Register page gradient -->
          <tr>
            <td style="background:linear-gradient(to bottom right,#052e16,#14532d,#166534);padding:36px 40px;text-align:center;">
              <table cellpadding="0" cellspacing="0" style="margin:0 auto;">
                <tr>
                  <td style="padding-right:8px;vertical-align:middle;">
                    <!-- Exact FlagIcon path from FlagIcon.tsx -->
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="white" xmlns="http://www.w3.org/2000/svg">
                      <path fill-rule="evenodd" clip-rule="evenodd" d="M3 2.25a.75.75 0 0 1 .75.75v.54l1.838-.46a9.75 9.75 0 0 1 6.725.738l.108.054A8.25 8.25 0 0 0 18 4.524l3.11-.732a.75.75 0 0 1 .917.81 47.784 47.784 0 0 0 .005 10.337.75.75 0 0 1-.574.812l-3.114.733a9.75 9.75 0 0 1-6.594-.77l-.108-.054a8.25 8.25 0 0 0-5.69-.625l-2.202.55V21a.75.75 0 0 1-1.5 0V3A.75.75 0 0 1 3 2.25Z" />
                    </svg>
                  </td>
                  <td style="vertical-align:middle;">
                    <span style="color:#ffffff;font-size:20px;font-weight:700;letter-spacing:-0.3px;">League Caddie</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:40px 40px 32px;">
              <!-- Eyebrow label — matches text-xs font-bold uppercase tracking-[0.15em] text-green-700 -->
              <p style="margin:0 0 8px;font-size:11px;font-weight:700;letter-spacing:0.15em;text-transform:uppercase;color:#15803d;">Password Reset</p>
              <h1 style="margin:0 0 16px;font-size:26px;font-weight:700;color:#111827;line-height:1.25;">Reset your password</h1>
              <p style="margin:0 0 28px;font-size:15px;color:#6b7280;line-height:1.6;">
                We received a request to reset the password for your League Caddie account.
                Click the button below to choose a new password. This link expires in <strong style="color:#111827;">{settings.RESET_TOKEN_EXPIRE_HOURS}&nbsp;hour(s)</strong>.
              </p>

              <!-- CTA button — matches bg-green-800 hover:bg-green-700 font-semibold py-3 px-6 rounded-xl -->
              <table cellpadding="0" cellspacing="0" style="margin:0 auto 28px;">
                <tr>
                  <td style="background-color:#166534;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,0.12);">
                    <a href="{reset_url}"
                       style="display:inline-block;padding:14px 32px;font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;border-radius:12px;">
                      Reset password
                    </a>
                  </td>
                </tr>
              </table>

              <p style="margin:0 0 6px;font-size:13px;color:#9ca3af;">Or copy and paste this link into your browser:</p>
              <p style="margin:0 0 28px;font-size:12px;word-break:break-all;">
                <a href="{reset_url}" style="color:#166534;text-decoration:none;">{reset_url}</a>
              </p>

              <!-- Divider -->
              <hr style="border:none;border-top:1px solid #e5e7eb;margin:0 0 24px;" />

              <p style="margin:0;font-size:13px;color:#9ca3af;line-height:1.5;">
                If you didn't request a password reset, you can safely ignore this email — your password won't change.
              </p>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color:#f9fafb;border-top:1px solid #e5e7eb;padding:20px 40px;text-align:center;">
              <p style="margin:0;font-size:12px;color:#9ca3af;">
                &copy; 2026 League Caddie &nbsp;&middot;&nbsp; You're receiving this because a reset was requested for your account.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    _ses_client().send_email(
        Source=settings.SES_FROM_EMAIL,
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": "Reset your League Caddie password"},
            "Body": {
                "Text": {"Data": text_body},
                "Html": {"Data": html_body},
            },
        },
    )
