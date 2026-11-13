# Pay-Per-Call Alert Center: Setup Guide (beginner friendly)

Follow the steps in order. Each step ends with a **✅ You should see** line. If you see that, go to the next step. If not, jump to **Troubleshooting** at the bottom.

Total time: about 45 minutes (30 if you are only upgrading, see the box below). You do not need to install anything on your computer.

---

## ⬆️ Already did the first setup? Upgrade in 4 steps (about 10 minutes)

1. **Replace the files** in GitHub with the new ones from the zip (same method as Step 2). You can delete `ringba_columns.json` (no longer used).
2. Streamlit: **⋮ → Reboot app**.
3. Open the app. It shows *"The database needs to be set up or upgraded"*. Press the button. Your calls, publishers, buyers, caps and emails are kept. Anything you set as a single affiliate manager is moved into the new "many managers" list.
4. **Settings** → fill in **Your app link** (your Streamlit address, for the answer buttons) → *Save settings*. Then **Settings → Test Ringba connection**.

Then press **🔄 Sync DIDs from Ringba now** on the DIDs page.
5. **Fix the missing calls (important).** The earlier version only collected 20 calls per request, so your stored days are incomplete. Run **Step 8** again (load 90 days; it fills in what is missing and never duplicates), then use **📑 Reports → 🩺 Check for missing or incomplete days** and **🔍 Compare with a Ringba report** to confirm the numbers now match.

6. **Update the engine workflow** (important if your 5-minute job was being cancelled): in GitHub open `.github/workflows/engine.yml`, press the pencil, replace everything with the contents of `workflows_to_paste/engine.yml` from the zip, and commit. Everything else in this round is in the files you already upload.

Then continue from **Step 8** below. New things to look at: the **Emails** page (sender + managers), the **Email templates** page (wording + answer buttons), and the **DIDs** page (import with creation dates).

---

## What you are building

```
Ringba  ──(every 5 min, GitHub)──►  Database (Neon)  ──►  Rules  ──►  Email (Gmail)
                                          ▲
                                          └──── Your screen (Streamlit app): reports, caps, emails, DIDs
```

Your old tables are **not deleted**. They are renamed `old_...` as a backup, and your publisher / buyer emails, watchlist, caps and admin email are copied into the new layout.

**Alerts you get**

| Alert | When |
|---|---|
| New publisher | Any call from a new publisher. A separate email for each of the first 10 calls (number is editable) |
| Publisher is back | A call after 30+ days of silence (days editable) |
| Watchlist buyer | A call **connected** to a buyer you ticked ⭐ |
| Buyer cap | Buyer's **paid** calls reach your % of the cap (default 80% and 100%) |
| Publisher cap | Publisher's **converted** calls reach your % of the cap |
| Inactive DID | A DID has had no calls for 30 days (also for new DIDs that never got a call) |
| Engine offline / Ringba error | The 5-minute job stopped or cannot read Ringba |

Every alert can be switched off, and every email address (To, CC, affiliate managers, extra CC) can be edited in the app.

---

## Step 1. Unzip the files

1. Download `p2c-alert-center.zip` and unzip it. You get a folder with files like `app.py`, `engine.py`, `schema.sql`.
2. Keep this folder open. Mac users: press `Cmd + Shift + .` to show hidden folders (you will see `.github`).

✅ You should see about 25 files and a few folders.

---

## Step 2. Replace the files in GitHub

1. Open your GitHub repository (the one Streamlit and the 5-minute job use).
2. **Delete the old files that are no longer used:** `alert_engine.py`, `emailer.py`.
   Click the file → trash-can icon (top right) → *Commit changes*.
3. **Stop the old 5-minute job.** In your repo open `.github/workflows/`, click the old workflow file (the one that runs `alert_engine.py`) → trash-can icon → *Commit changes*.
4. Upload the new files: **Add file → Upload files**, drag in **everything from the unzipped folder except** the folders `.github` and `workflows_to_paste`. Files with the same name are replaced. Press *Commit changes*.
   - Folders: also drag in `tests` and `.streamlit` if your browser allows it. They are optional.
5. Add the two automation files:
   **Add file → Create new file**. In the name box type exactly `.github/workflows/engine.yml` (typing the `/` makes the folders). Open `workflows_to_paste/engine.yml` from your unzipped folder, copy everything, paste, press *Commit changes*.
   Repeat for `.github/workflows/backfill.yml`.

✅ In your repo you now see `app.py`, `engine.py`, `schema.sql`, `requirements.txt`, `requirements-engine.txt`, and `.github/workflows` has `engine.yml` and `backfill.yml`.

> **Keep the repository Public.** GitHub's free plan gives unlimited run-minutes only for public repos (a private repo would run out in about a week at 288 runs a day). Your passwords are never in the code. They live in *Secrets* (next steps).

---

## Step 3. Put your secrets in GitHub

Repo → **Settings → Secrets and variables → Actions → New repository secret**. Create (or check that they exist, with exactly these names):

| Name | Value |
|---|---|
| `DATABASE_URL` | Your Neon connection string (Neon dashboard → *Connection details* → copy). It starts with `postgresql://` |
| `RINGBA_API_TOKEN` | Your Ringba API token |
| `RINGBA_ACCOUNT_ID` | Your Ringba account id (starts with `RA`) |
| `SMTP_USER` | Your Gmail address |
| `SMTP_PASSWORD` | The 16-character Gmail App Password (no spaces) |

You can delete an old secret called `DRY_RUN` if you have one. The on/off switch is now inside the app.

✅ You see five secrets listed (values are hidden, that is normal).

---

## Step 4. Put your secrets in Streamlit

1. Go to your app on share.streamlit.io → **⋮ (three dots) → Settings → Secrets**.
2. Paste this, with your real values:

```toml
DATABASE_URL = "postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require"
RINGBA_API_TOKEN = "your_ringba_api_token"
RINGBA_ACCOUNT_ID = "RAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
SMTP_USER = "yourgmail@gmail.com"
SMTP_PASSWORD = "your16characterapppassword"
APP_PASSWORD = "choose-a-password-for-your-screen"
```

   `APP_PASSWORD` is strongly recommended: without it anyone who finds your app link can see your calls.
3. Save. Then **⋮ → Reboot app**. Make sure the app's *Main file path* is `app.py`.

✅ After a minute the app opens and asks for your password.

---

## Step 5. Create the new database layout

1. Open your app and enter the password.
2. You see a blue box: *"First time here…"*. Press **🛠️ Set up / upgrade the database now**.

✅ You see a log like *"Found your OLD tables. Keeping them as a backup… copied N publishers… DONE"* and then the app loads with 10 tabs.

---

## Step 6. Test the Ringba connection

1. Tab **⚙️ Settings** → press **🔌 Test Ringba connection**.

✅ You should see green *"Ringba works. Read N calls…"*. The line *"First call read"* must show the **DID as a number you own** (not the caller's number).

The app talks to Ringba's standard call-log endpoint (`POST /v2/{accountId}/calllogs`), 500 calls per page, with a 1-second pause between requests so Ringba does not block you (HTTP 429). If Ringba still says "too many requests", the app waits and retries by itself.

If it is red, read the message and see Troubleshooting.

---

## Step 7. Set your email addresses and test them

Tab **✉️ Emails & Alerts**:

1. **Admin emails**: type your email(s), one per line → *Save admin emails*.
2. Press **📨 Send a test email now**.

✅ An email arrives in your inbox (check Spam the first time).

---

## Step 8. Load 90 days of history

History is loaded **without sending any alerts**. You can run it again safely.

**Option A (simple):** page **🔄 Sync & Import** → days = 90 → **⬇️ Load history now**. With about 800 calls a day (read 100 at a time, with a 1-second pause between requests so Ringba does not block you), 90 days takes roughly 15 to 25 minutes. Option B below is better for this.

**Option B (runs in the background, recommended):** GitHub repo → **Actions** tab → **Load history from Ringba (run by hand)** → **Run workflow** → days `90` → *Run workflow*. Wait for the green tick (about 15 to 25 minutes). You can close your browser meanwhile.

**Check the numbers (do this once):**
Tab **📑 Reports** → *Yesterday*. Compare **Calls** with Ringba's own report for yesterday (same timezone). They should match. Also check *Last 7 days*.

✅ Numbers match Ringba. Tab **📞 DIDs** now lists your DIDs, sorted by "Days without calls".

---

## Step 9. Fill in your data

Do these **before** going live so your first emails are meaningful.

1. **✉️ Emails → Affiliate managers**: add each manager (name + email). Press **Save changes** once after adding all of them.
2. **🧑‍💼 Publishers**: below the table, use **Affiliate managers on CC**: choose one or many publishers, choose 1, 2, 3 or more managers, press *Apply*. All of them are copied on that publisher's alerts. Optionally add the publisher's own email and tick *CC the publisher?*, and extra CC emails (comma-separated).
3. **🏢 Buyers**: tick ⭐ **Watchlist** for buyers you want connect alerts on. You can add a buyer by name even before it has calls.
4. **📞 DIDs**: you do not have to type them. The engine reads your account's number list from Ringba (`GET /v2/{accountId}/numbers`) about once an hour, so every DID, including newly created ones and ones that never got a call, appears with its **Ringba creation time**. Press **🔄 Sync DIDs from Ringba now** the first time: the green message tells you how many numbers it found and which Ringba fields it used. A DID you delete in Ringba is marked **retired** automatically (you can switch that off in Settings; a safety guard refuses to retire a big share at once). The 30-day clock starts from the DID's Ringba creation time (its allocation date). Each DID also shows the **publisher it is assigned to in Ringba** and its **next charge date**. The inactive-DID email includes both, and copies that publisher's affiliate managers. **If you upgraded from the first version:** open **✉️ Emails**, find *Inactive DID* in the bottom table, tick **CC affiliate managers**, and press Save. The paste / upload box stays available as a backup.
   The DID table shows, in this order: **Campaign name, Publisher Name, DID Created Date, Next charge, DID, Last Call Date, Total calls last 30 days, Follow up note from AM** (then Case, Status, Label, cost and team notes). Look at the list sorted by *Days without calls*. Any DID you already know is dead or already cancelled: set its status to **retired**, press **Save changes**, and it never emails you.
5. **🚦 Caps**: copy your Ringba caps into *Buyer caps* (counts **paid** calls) and *Publisher caps* (counts **converted** calls). The column **Used now / % now** lets you compare with Ringba. You can change what each cap counts (paid / converted / connected / all) and the alert percentages (e.g. `80,100`).
6. **✉️ Emails → table at the bottom**: for each alert type you can switch it on/off, change the days/count, choose who is copied, and add "Always CC" emails.

7. **✉️ Emails → Sender**: set the sender name (for example *Acme Call Alerts*) and the reply-to address (your team inbox). Emails go out through the mailbox in your Secrets (`SMTP_USER`). To send from a company address, put that company mailbox and its app password in the Secrets (Google Workspace and Microsoft 365 both work; for Microsoft use `SMTP_HOST = "smtp.office365.com"`, port 587). Use **Send a test email** to check.
8. **📝 Email templates**: change the subject and message of any alert, save several versions, and choose which one is in use. Press **Preview** to see it with example data. Add **answer buttons** (see the next section).

---

## Answer buttons: let managers reply from the email

Example for an inactive DID: the email says *"DID +1614… has had no calls for 34 days"* and shows buttons such as **Remove this DID**, **Keep it - wait 7 more days**, **I will contact the publisher first**.

How it works:
1. A manager (CC'd) clicks a button in the email. Your app opens a small page, they type their name and an optional note, and press **Confirm**. No password is needed for that page; the link is secret and works once.
2. The answer is saved on the alert (see **📜 Alert Log**), and your admin emails get a notification: *"Mia answered: Keep it - wait 7 more days"*.
3. Each button has an **effect** you choose on the **📝 Email templates** page:
   - **record**: only record the answer. Your team decides what to do (for example delete the DID in Ringba).
   - **snooze**: also pause that alert for N days (the DID or cap will not alert again until then).
   - **retire_did**: also mark the DID retired in this tool.
4. Edit, add or remove buttons for every alert type on the same page. Remove all buttons to send a plain email.

Requirements: put your Streamlit address in **Settings → Your app link**. If your app has gone to sleep, the first click can take about 30 seconds to wake it up.

---

## Email formatting: bold, bullets, numbers, headings, links

On the **📝 Email templates** page the message box understands a few simple marks. Type them, press **👁 Preview**, and you see the finished email:

| You type | The email shows |
|---|---|
| `**bold text**` | **bold text** |
| `*italic*` and `__underlined__` | italic and underlined text |
| `# Big heading` / `## Smaller heading` | a heading |
| `- a bullet` (or `* a bullet`) | a bullet list. Start a line with two spaces first for a sub-bullet |
| `1. first step` `2. second step` | a numbered list |
| `[click here](https://example.com)` | a link |
| `---` | a dividing line |
| an empty line | a new paragraph |

Placeholders like `{publisher}` work inside any of these (for example `**{publisher}**`). The email goes out in two versions, formatted and plain text, so it also reads well in mail apps that do not show formatting. Names and values coming from Ringba can never break the layout. The built-in templates already use bold labels and bullet lists; a built-in template you never edited is upgraded to the new look automatically, and one you edited is never touched.

---

## Team status on every alert

On the **📜 Alert Log** page each row has a **Team status** dropdown right in the table:

🆕 Open (no answer yet) · 💬 Answered - needs action · ⏳ Pending (work not started) · 🔧 In progress · 🕒 Waiting for publisher / AM · ✅ Done (action completed) · 🚫 No action needed

When an affiliate manager answers (button or email reply) the alert moves to *Answered - needs action*, so you can see what the team still has to do. After you did what the answer says, set it to *Done*. Change as many rows as you like, optionally type a note in **Add a note**, add your name, and press **Save status / notes** once. The table also shows, for every alert, the manager's answer, the **Note for the team** they typed on the answer page, and the latest email reply. Everything is kept in the history. The DID page shows the same status in its **Case** column.

---

## Reading the engine log on GitHub

Every line of the 5-minute job now starts with the seconds since the run began, and each step is printed BEFORE it runs:

```
[   0.2s] checking the engine heartbeat ...
[   0.4s] reading calls from Ringba ...
[   3.1s] Ringba sync: fetched 214, new 9, updated 205
[   3.2s] refreshing the DID list from Ringba (once an hour) ...
[   4.0s] DID list: not due yet
[   4.1s] checking the alert rules ...
[   4.6s] sending emails ...
[   7.9s] emails: sent=3, dry_run=0, failed=0, waiting=0, left=0
[   8.0s] done in 8.0s
```

A normal run takes seconds. The job can no longer hang: Ringba and the mailbox have time limits, emails go out over one Gmail login (at most 25 per run, the rest on the next runs), and a watchdog stops a stuck run after about 6.5 minutes with a line starting **WATCHDOG** that names the step it was stuck in and shows where every part of the program was. If you ever see WATCHDOG, send me that part of the log.

---

## Follow-up notes, email replies and backup

**What the inactive-DID email contains:** the DID, publisher, campaign, last call on that DID, total calls in the last 30 days, Ringba creation date, next charge date, monthly cost, and the latest follow-up note from the affiliate manager. Every email also gets the answer buttons described above.

**How the affiliate manager's answer reaches the DID table (3 ways):**
1. **A button + note** on the answer page (see *Answer buttons*).
2. **A normal email reply.** Every alert subject ends with a tag like `[#A123]`. When a CC'd manager replies, the engine reads the reply (from the same mailbox that sends the alerts), cuts away the quoted original, and saves the text. Only people who were on that alert's To / CC are accepted. For this to work: **leave Reply-to empty** on the *Emails* page (replies must come back to the mailbox that sends the alerts, because that is the only mailbox the tool reads), make sure **IMAP is switched on** for that mailbox (Gmail: *Settings → See all settings → Forwarding and POP/IMAP → Enable IMAP*; Google Workspace admins may need to allow it; Microsoft 365 also supports it). Check **Alert Log**: it shows when the mailbox was last read, or the problem. Replies are only accepted from an address that was on that alert's To or CC list, only if the subject still contains the `[#A123]` tag, and a reply sent from the alert mailbox's own address is ignored (so to test, reply from a different address that is on CC, for example a second admin email or an affiliate manager). If the login fails, the tool waits 30 minutes before trying again.
3. **A note typed by your team** on the DID page (*History of a DID*) or the Alert Log (*Follow-up / done note*), where you can also mark the alert **Open / Follow-up / Done**.

The newest answer or reply from the manager appears in **Follow up note from AM**; your team's own notes stay separate (**Team notes** and the history).

**Nothing is ever deleted.** Every alert, answer, reply and note is kept in the database, so you can review old decisions. In addition:
- **Alert Log → 🗄️ Prepare backup (ZIP)** gives you a zip of CSV files (alerts, notes, DIDs, publishers, buyers, caps, templates).
- The engine **emails that zip to your admin addresses every 7 days** (change or switch off in *Settings*, "Email me a backup every N days").

---

Daily caps reset at 00:00 New York time by default (the day ends at 11:59 PM). Change it in Settings → *Day starts at*.

---

## Step 10. Test with DRY RUN (nothing is really sent)

DRY RUN is ON by default (you see a yellow bar at the top).

1. Page **🔄 Sync & Import → ▶️ Run engine now**.
2. Page **📜 Alert Log**: alerts are listed with status `dry_run`. Open one with *Read one alert* to see exactly what would be sent, and who is in To and CC.

If you see alerts you do not want (for example inactive DIDs you forgot to retire), fix the data and they will not repeat.

---

## Step 11. Go live

1. Tab **⚙️ Settings** → tick **✅ Send REAL emails** → *Save settings*. The yellow bar disappears.
2. GitHub → **Actions → Alert engine (every 5 minutes) → Run workflow**. Wait for the green tick.
3. From now on GitHub runs it every 5 minutes by itself. Check the dashboard: **Last good sync** should say "x min ago".

✅ Done. 🎉

---

## How it behaves (good to know)

- **Fast editing.** Every table is a form: change as many cells as you like, then press **Save changes** once. Nothing reloads until you press it. Only the page you are looking at loads. The first click after the app has been idle can take a few seconds because the free database wakes up.

- **Nothing is sent twice.** Every alert has a unique key. An alert is marked *sent* only after Gmail accepts it. If Gmail fails, it retries on the next run (up to 5 times), then shows as `failed` in the Alert Log, where you can press *Retry failed alerts*.
- **History never alerts.** Only calls first seen by the live 5-minute job can create alerts.
- **Calls are refreshed.** Each run re-reads the last 6 hours, so calls that were still live get their final connected / converted / paid status.
- **Inactive-DID emails repeat** every 30 days of continued silence (at 30, 60, 90 ...). Set the DID to *retired* to stop them.
- **Cap alerts:** one email per cap, per window (day/month), per level. If a cap jumps from 70% to 100% you get one email, not two. Editing a cap's number starts fresh.
- **"New publisher" depends on your stored history.** A publisher that has not called for more than 90 days looks new. This is why you loaded 90 days.
- GitHub can delay the 5-minute schedule by a few minutes, and **pauses scheduled jobs in repos with no activity for 60 days**. If emails stop, open Actions and press *Enable workflow* (or the dashboard will show a red "no successful sync" warning).

---

## Troubleshooting

| What you see | What to do |
|---|---|
| App says *DATABASE_URL is missing* | Step 4: add it to **Streamlit** secrets, then Reboot |
| *Ringba refused the login* | Wrong `RINGBA_API_TOKEN` or `RINGBA_ACCOUNT_ID`. Re-copy them (no spaces, no quotes) |
| *Ringba says not found* | `RINGBA_ACCOUNT_ID` is wrong (starts with `RA`) |
| *Ringba returned JSON instead of a CSV* | Send me the message shown. It means Ringba answered in a different format |
| *file is missing columns: …* | Send me the message shown, plus the first line of a Ringba CSV export |
| Test email: *Username and Password not accepted* | Use a Gmail **App Password** (16 characters), with 2-step verification on. Not your normal password |
| Alerts are in the log as `dry_run` | DRY RUN is on. Settings → tick *Send REAL emails* → Save |
| Alerts stay `pending` | No admin email saved (Emails & Alerts tab) |
| *Ringba rejected the request* or *Ringba's answer is missing …* | Settings → Test Ringba connection, then send me the message and the list of keys it shows |
| *DID sync failed* / *could not find the phone number field* | Send me the message. It lists the field names Ringba returned, so I can map them |
| Answer buttons missing from emails | Settings → fill in *Your app link*, and check the alert type still has buttons on the Email templates page |
| GitHub run says *The operation was canceled* after 8 minutes | Update `engine.yml` (upgrade step 6) and upload the new files. Open the next run's log: the last line shows which step was slow. If it says WATCHDOG, send it to me |
| Report does not match Ringba | Reports → *Compare with a Ringba report* with the SAME dates and timezone. If the tool is lower, *Check for missing or incomplete days* → *Reload these days* |
| *Could not read email replies* | Turn IMAP on for the mailbox (see above), leave Reply-to empty, or untick *Read email replies* in Settings |
| Alerts show `failed` | Read the reason in the Alert Log, fix it, press *Retry failed alerts* |
| Dashboard says *No successful sync for N minutes* | GitHub → Actions → open the latest *Alert engine* run and read the red step. Most common: a secret name is misspelled |
| Backfill says some days failed | Run it again. It only fills what is missing |
| Report says part of the range is not in the database | Press *Load the missing part from Ringba* in the Reports tab |

If anything is unclear, send me a screenshot of the message and the step number.


---

## Update notes (Nov 2026)

* **Engine no longer hangs on Neon.** `db.py` now sets TCP keep-alives and a 15 s dead-connection limit, a 30 s limit per query, and retries a dead connection twice. The rules read the publisher history with ONE query instead of two per call.
* **What happens if nobody answers an inactive-DID email?** A REMINDER is sent every `did_reminder_days` (default 3) days, at most `did_reminder_max` (default 3) times. Both are in Settings. After the last reminder nothing more is sent until the DID reaches the next idle period (2 x the Days of the rule). If someone clicks a 'wait N days' button, a FOLLOW-UP is sent when the pause ends. Nothing is ever paused for ever, and every email says this at the bottom.
* **Never alert:** tick *Never alert* on the DIDs page. That DID gets no inactive-DID emails and its calls trigger no new-publisher / returning-publisher / watchlist emails. (Caps still count its calls, because a cap is about the publisher or buyer, not the number.)
* **Test alerts:** Alert Log -> *Delete test alerts*. An alert that exists blocks a new one for the same DID, so delete the test alerts before you go live, then untick DRY RUN in Settings.
* The first engine run after this update adds the new database column by itself; opening the app once does the same.

### Caps (second update)

* **Buyer target caps are automatic.** The engine reads every target's daily and monthly cap from Ringba (`GET /targets`, the `schedule` object) and its live usage (`GET /targets/{id}/Counts`: `currentDay`, `currentMonth`). Alerts at the % levels you choose (default 80,100) for BOTH the daily and the monthly cap. See Caps -> "Buyer target caps". Targets are re-read every hour (Settings key `targets_sync_minutes`), usage on every 5-minute run. Usage older than 30 minutes is never used for an alert.
* **Publisher caps are per campaign.** Ringba has no cap data for publishers, so they are typed in. Pick the Campaign on each row (one row per campaign); usage is counted from your own calls for that publisher + campaign.
* The first engine run after the update creates the new `targets` table and the `campaigns` column by itself.
