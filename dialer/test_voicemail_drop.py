"""Fire a test outbound call so you can verify AMD + voicemail-drop end-to-end
WITHOUT needing the dialer UI to be working.

Usage:
  cd dialer
  python test_voicemail_drop.py +14045551234
  # don't answer the phone — let it ring to voicemail
  # AMD will fire, /twilio/amd will play your recorded drop, call ends

Requires in .env:
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_FROM_NUMBER
  DIALER_PUBLIC_BASE_URL    (must be HTTPS — your cloudflared tunnel)
  DIALER_AMD_ENABLED=1
  DIALER_VOICEMAIL_DROP_URL=https://.../voicemail-drop.mp3
"""
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / "claude code" / ".env", override=True)

from twilio.rest import Client


def main():
    if len(sys.argv) < 2:
        print("Usage: python test_voicemail_drop.py +1XXXXXXXXXX")
        print("  (use a number you can let ring to voicemail, like your own cell)")
        sys.exit(1)

    to_number = sys.argv[1].strip()

    sid    = os.environ["TWILIO_ACCOUNT_SID"]
    token  = os.environ["TWILIO_AUTH_TOKEN"]
    from_  = os.environ["TWILIO_FROM_NUMBER"]
    base   = os.environ.get("DIALER_PUBLIC_BASE_URL", "").rstrip("/")
    drop_url = os.environ.get("DIALER_VOICEMAIL_DROP_URL", "")

    if not base or not base.startswith("https://"):
        print(f"ERROR: DIALER_PUBLIC_BASE_URL must be HTTPS, got: {base!r}")
        sys.exit(1)
    if not drop_url:
        print("WARNING: DIALER_VOICEMAIL_DROP_URL is empty — AMD will fire but no audio plays.")

    session_id = f"test-vmd-{int(time.time())}"
    amd_cb = f"{base}/twilio/amd?session_id={session_id}"

    # Initial TwiML — long pause so the call stays open while AMD analyses
    # and our /twilio/amd handler has time to update the call with the drop.
    # If AMD says "human", they'll hear silence for ~60s then we hang up.
    twiml = (
        '<Response>'
        '<Pause length="60"/>'
        '<Hangup/>'
        '</Response>'
    )

    print(f"Dialing {to_number} from {from_} ...")
    print(f"AMD callback: {amd_cb}")
    print(f"Drop URL:     {drop_url or '(unset — will just log the AMD verdict)'}")
    print()

    client = Client(sid, token)
    call = client.calls.create(
        to=to_number,
        from_=from_,
        twiml=twiml,
        machine_detection="DetectMessageEnd",
        async_amd="true",
        async_amd_status_callback=amd_cb,
        async_amd_status_callback_method="POST",
        machine_detection_timeout=30,
        machine_detection_speech_threshold=2400,
        machine_detection_speech_end_threshold=1200,
        machine_detection_silence_timeout=5000,
        timeout=30,
    )
    print(f"Call SID:     {call.sid}")
    print(f"Status:       {call.status}")
    print()
    print("Watch the call live in Twilio console:")
    print(f"  https://console.twilio.com/us1/monitor/logs/calls/{call.sid}")
    print()
    print("What to do now:")
    print("  1. Let it ring to voicemail (DON'T answer)")
    print("  2. AMD will fire ~5-15s after your voicemail greeting starts")
    print("  3. Your /twilio/amd handler updates the call with <Play>drop.mp3</Play><Hangup/>")
    print("  4. Watch tail of Flask logs for 'Twilio AMD: machine_end_beep' event")


if __name__ == "__main__":
    main()
