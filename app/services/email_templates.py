"""Shared, email-client-friendly Tanoti layout. Body must already be escaped."""
from html import escape


def layout(title, body):
    return f'''<!doctype html><html><body style="margin:0;background:#f5f2ed;color:#282520;font-family:Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#fff;border:1px solid #e6e0d7;border-radius:12px">
<tr><td style="padding:32px"><div style="font-family:Georgia,serif;font-size:28px;letter-spacing:3px">TANOTI</div>
<p style="font-size:11px;letter-spacing:2px;color:#756c60">RETURNS &amp; EXCHANGES</p>
<h1 style="font-family:Georgia,serif;font-size:25px;font-weight:normal;margin:28px 0 18px">{escape(title)}</h1>
<div style="font-size:15px;line-height:1.7">{body}</div>
<p style="border-top:1px solid #e6e0d7;padding-top:20px;margin-top:28px;font-size:12px;color:#756c60">Thank you for shopping with Tanoti.<br>For help, please contact the store and quote your request or order number.</p>
</td></tr></table></td></tr></table></body></html>'''


def login_email(code, minutes, admin=False):
    title = "Your admin sign-in code" if admin else "Your sign-in code"
    body = f'''<p>Use this one-time code to sign in to Tanoti {'admin' if admin else 'Returns'}.</p>
<p style="background:#f5f2ed;border-radius:8px;padding:20px;text-align:center;font-size:30px;letter-spacing:8px;font-weight:bold">{escape(code)}</p>
<p>This code expires in {int(minutes)} minutes. Do not share it with anyone.</p>
<p style="color:#756c60;font-size:13px">If you did not request this code, you can ignore this email.</p>'''
    return layout(title, body)
