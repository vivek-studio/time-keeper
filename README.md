# Message Hours v1.0.0
Import one or more Slack List JSON exports, analyse working hours per user/day, check data health, push to Google Sheets.

Run: `start.bat` (Windows) or `./start.sh`, then open http://127.0.0.1:8765 (opens automatically).
Settings, excluded IDs and names live in a local SQLite file (`%APPDATA%\Message Hours\message_hours.db`).

Columns: delayed checkout (last message after 20:00), next working day started at 11 (first message 11:00-11:59), at 12 (12:00-12:59).
All thresholds are in Settings. Hours are a proxy: first/last Slack message of the day, not Zoho check-ins.
